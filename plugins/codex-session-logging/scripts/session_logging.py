#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

JsonDict = dict[str, Any]

STATE_DIR_ENV = "CODEX_SESSION_LOG_STATE_DIR"
SUPABASE_URL_ENV = "CODEX_SESSION_LOG_SUPABASE_URL"
SUPABASE_KEY_ENV = "CODEX_SESSION_LOG_SUPABASE_SERVICE_ROLE_KEY"
SUPABASE_USER_ID_ENV = "CODEX_SESSION_LOG_USER_ID"
SUPABASE_BUCKET_ENV = "CODEX_SESSION_LOG_BUCKET"
DEFAULT_SUPABASE_URL = "https://pmdfllwuctzkdjiehezq.supabase.co"
DEFAULT_BUCKET = "codex-sessions"
ALLOWED_GITHUB_ORG = "e3-solutions"
EXCERPT_BYTES = 4096


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Capture and upload Codex session logging events.")
    subparsers = parser.add_subparsers(dest="command")
    capture_parser = subparsers.add_parser("capture", help="Capture one hook payload from stdin.")
    capture_parser.add_argument("--event", help="Hook event name override.")
    subparsers.add_parser("drain", help="Upload queued records to Supabase.")
    args = parser.parse_args(argv)

    if args.command == "drain":
        result = drain_queue()
        print(json.dumps(result, sort_keys=True))
        return

    payload = read_stdin_json()
    captured = capture_hook_event(payload, event_name=getattr(args, "event", None))
    if captured:
        print(json.dumps({"captured": captured["id"], "role": captured["role"]}, sort_keys=True))


def read_stdin_json() -> JsonDict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
    return loaded if isinstance(loaded, dict) else {"payload": loaded}


def capture_hook_event(payload: JsonDict, *, event_name: str | None = None) -> JsonDict | None:
    hook_event = event_name or str(payload.get("hook_event_name") or payload.get("event") or "")
    role, content = message_from_payload(hook_event, payload)
    if not role or content is None:
        return None
    if not should_capture_payload(payload):
        return None

    base = ensure_state_dir()
    session_id = safe_segment(first_string(payload, "session_id", "sessionId") or "unknown-session")
    turn_id = first_string(payload, "turn_id", "turnId")
    seq = next_sequence(base, session_id)
    user_key = safe_segment(os.environ.get(SUPABASE_USER_ID_ENV) or "local")
    content_bytes = content.encode("utf-8")
    content_hash = hashlib.sha256(content_bytes).hexdigest()
    storage_path = f"users/{user_key}/sessions/{session_id}/messages/{seq:06d}-{role}.json"
    local_content_path = storage_path
    created_at = now_iso()
    metadata = metadata_from_payload(payload)
    message = {
        "id": uuid.uuid4().hex,
        "session_id": session_id,
        "turn_id": turn_id,
        "seq": seq,
        "role": role,
        "content": content,
        "content_sha256": content_hash,
        "content_byte_size": len(content_bytes),
        "hook_event_name": hook_event,
        "created_at": created_at,
        "metadata": metadata,
    }
    write_json_atomic(base / local_content_path, message)
    append_jsonl(base / f"users/{user_key}/sessions/{session_id}/raw.jsonl", {"payload": payload, "captured_at": created_at})

    event = {
        "id": message["id"],
        "type": "message",
        "session_id": session_id,
        "turn_id": turn_id,
        "seq": seq,
        "role": role,
        "hook_event_name": hook_event,
        "storage_bucket": bucket_name(),
        "storage_path": storage_path,
        "local_content_path": local_content_path,
        "content_sha256": content_hash,
        "content_byte_size": len(content_bytes),
        "content_excerpt": utf8_excerpt(content_bytes),
        "metadata": metadata,
        "created_at": created_at,
        "uploaded_at": None,
    }
    append_jsonl(base / "events.jsonl", event)
    append_jsonl(base / "queue.jsonl", event)
    try_auto_drain()
    return event


def message_from_payload(hook_event: str, payload: JsonDict) -> tuple[str | None, str | None]:
    if hook_event == "UserPromptSubmit":
        return "user", first_string(payload, "prompt")
    if hook_event == "Stop":
        return "assistant", first_string(payload, "last_assistant_message", "lastAssistantMessage")
    return None, None


def metadata_from_payload(payload: JsonDict) -> JsonDict:
    metadata: JsonDict = {}
    for key in ("cwd", "transcript_path", "model", "source"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            metadata[key] = value
    return metadata


def should_capture_payload(payload: JsonDict) -> bool:
    cwd = first_string(payload, "cwd") or os.getcwd()
    remote = git_origin_remote(cwd)
    return remote_belongs_to_org(remote, ALLOWED_GITHUB_ORG)


def git_origin_remote(cwd: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", cwd, "remote", "get-url", "origin"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        return None
    remote = result.stdout.strip()
    return remote or None


def remote_belongs_to_org(remote: str | None, org: str) -> bool:
    if not remote:
        return False
    value = remote.strip()
    patterns = (
        rf"^https://github\.com/{re.escape(org)}/[^/]+(?:\.git)?/?$",
        rf"^git@github\.com:{re.escape(org)}/[^/]+(?:\.git)?$",
        rf"^ssh://git@github\.com/{re.escape(org)}/[^/]+(?:\.git)?$",
    )
    return any(re.match(pattern, value, flags=re.IGNORECASE) for pattern in patterns)


def first_string(payload: JsonDict, *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return None


def ensure_state_dir() -> Path:
    base = state_dir()
    base.mkdir(parents=True, exist_ok=True)
    return base


def state_dir() -> Path:
    override = os.environ.get(STATE_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return Path.home() / ".codex" / "session-logging"


def next_sequence(base: Path, session_id: str) -> int:
    path = base / "sessions" / session_id / "sequence.txt"
    try:
        current = int(path.read_text(encoding="utf-8").strip() or "0")
    except (FileNotFoundError, OSError, ValueError):
        current = 0
    value = current + 1
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"{value}\n", encoding="utf-8")
    return value


def utf8_excerpt(content_bytes: bytes, *, limit: int = EXCERPT_BYTES) -> str:
    return content_bytes[:limit].decode("utf-8", errors="replace")


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def safe_segment(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "._-" else "-" for char in value.strip())
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    return cleaned or "unknown"


def bucket_name() -> str:
    return os.environ.get(SUPABASE_BUCKET_ENV) or DEFAULT_BUCKET


def write_json_atomic(path: Path, payload: JsonDict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def append_jsonl(path: Path, payload: JsonDict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, sort_keys=True) + "\n")


def drain_queue() -> JsonDict:
    base = ensure_state_dir()
    queue_path = base / "queue.jsonl"
    records = read_jsonl(queue_path)
    if not records:
        return {"uploaded": 0, "failed": 0, "remaining": 0}

    uploader = SupabaseUploader.from_env()
    remaining: list[JsonDict] = []
    uploaded = 0
    failed = 0
    for record in records:
        try:
            uploader.upload_message(record, base=base)
        except Exception as exc:  # noqa: BLE001 - hook uploader must preserve queue on any failure.
            failed += 1
            record["last_upload_error"] = str(exc)
            record["last_upload_failed_at"] = now_iso()
            remaining.append(record)
        else:
            uploaded += 1
    rewrite_jsonl(queue_path, remaining)
    return {"uploaded": uploaded, "failed": failed, "remaining": len(remaining)}


def try_auto_drain() -> None:
    if not upload_configured():
        return
    try:
        drain_queue()
    except Exception as exc:  # noqa: BLE001 - capture must not fail because remote upload is unavailable.
        append_jsonl(ensure_state_dir() / "upload_errors.jsonl", {"created_at": now_iso(), "error": str(exc)})


def upload_configured() -> bool:
    return bool(os.environ.get(SUPABASE_KEY_ENV) and os.environ.get(SUPABASE_USER_ID_ENV))


def read_jsonl(path: Path) -> list[JsonDict]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return []
    records: list[JsonDict] = []
    for line in lines:
        if not line.strip():
            continue
        try:
            loaded = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(loaded, dict):
            records.append(loaded)
    return records


def rewrite_jsonl(path: Path, records: list[JsonDict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(record, sort_keys=True) + "\n" for record in records), encoding="utf-8")


class SupabaseUploader:
    def __init__(self, *, supabase_url: str, service_role_key: str, user_id: str, bucket: str) -> None:
        self.supabase_url = supabase_url.rstrip("/")
        self.service_role_key = service_role_key
        self.user_id = user_id
        self.bucket = bucket

    @classmethod
    def from_env(cls) -> "SupabaseUploader":
        key = os.environ.get(SUPABASE_KEY_ENV)
        user_id = os.environ.get(SUPABASE_USER_ID_ENV)
        if not key:
            raise RuntimeError(f"{SUPABASE_KEY_ENV} is required to upload Codex session logs")
        if not user_id:
            raise RuntimeError(f"{SUPABASE_USER_ID_ENV} is required to upload Codex session logs")
        return cls(
            supabase_url=os.environ.get(SUPABASE_URL_ENV) or DEFAULT_SUPABASE_URL,
            service_role_key=key,
            user_id=user_id,
            bucket=bucket_name(),
        )

    def upload_message(self, record: JsonDict, *, base: Path) -> None:
        content_path = base / str(record["local_content_path"])
        content = content_path.read_bytes()
        self.storage_upload(str(record["storage_path"]), content)
        session_row = {
            "id": record["session_id"],
            "user_id": self.user_id,
            "storage_prefix": f"users/{self.user_id}/sessions/{record['session_id']}",
            "metadata": record.get("metadata") or {},
            "updated_at": now_iso(),
        }
        self.rest_upsert("codex_sessions", session_row, conflict="id")
        message_row = {
            "id": record["id"],
            "session_id": record["session_id"],
            "user_id": self.user_id,
            "turn_id": record.get("turn_id"),
            "seq": record["seq"],
            "role": record["role"],
            "storage_bucket": self.bucket,
            "storage_path": record["storage_path"],
            "content_sha256": record["content_sha256"],
            "content_byte_size": record["content_byte_size"],
            "content_excerpt": record.get("content_excerpt"),
            "metadata": record.get("metadata") or {},
            "created_at": record["created_at"],
        }
        self.rest_insert("codex_session_messages", message_row)

    def storage_upload(self, path: str, content: bytes) -> None:
        quoted_path = "/".join(urllib.parse.quote(part, safe="") for part in path.split("/"))
        url = f"{self.supabase_url}/storage/v1/object/{urllib.parse.quote(self.bucket, safe='')}/{quoted_path}"
        self.request(
            url,
            method="POST",
            data=content,
            headers={"content-type": "application/json", "x-upsert": "true"},
        )

    def rest_upsert(self, table: str, row: JsonDict, *, conflict: str) -> None:
        url = f"{self.supabase_url}/rest/v1/{table}?on_conflict={urllib.parse.quote(conflict)}"
        self.request(
            url,
            method="POST",
            data=json.dumps(row).encode("utf-8"),
            headers={
                "content-type": "application/json",
                "prefer": "resolution=merge-duplicates,return=minimal",
            },
        )

    def rest_insert(self, table: str, row: JsonDict) -> None:
        url = f"{self.supabase_url}/rest/v1/{table}"
        self.request(
            url,
            method="POST",
            data=json.dumps(row).encode("utf-8"),
            headers={"content-type": "application/json", "prefer": "return=minimal"},
        )

    def request(self, url: str, *, method: str, data: bytes, headers: dict[str, str]) -> bytes:
        merged_headers = {
            "apikey": self.service_role_key,
            "authorization": f"Bearer {self.service_role_key}",
            **headers,
        }
        request = urllib.request.Request(url, data=data, headers=merged_headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Supabase request failed {exc.code}: {body}") from exc


if __name__ == "__main__":
    main()
