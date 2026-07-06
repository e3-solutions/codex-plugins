#!/usr/bin/env python3
from __future__ import annotations

import argparse
import contextlib
import fcntl
import getpass
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

JsonDict = dict[str, Any]

STATE_DIR_ENV = "CODEX_SESSION_LOG_STATE_DIR"
SUPABASE_BUCKET_ENV = "CODEX_SESSION_LOG_BUCKET"
INGEST_URL_ENV = "CODEX_SESSION_LOG_INGEST_URL"
INGEST_TOKEN_ENV = "CODEX_SESSION_LOG_INGEST_TOKEN"
AUTO_UPLOAD_ENV = "CODEX_SESSION_LOG_AUTO_UPLOAD"
DEFAULT_SUPABASE_URL = "https://pmdfllwuctzkdjiehezq.supabase.co"
DEFAULT_INGEST_URL = f"{DEFAULT_SUPABASE_URL}/functions/v1/codex-session-ingest"
DEFAULT_BUCKET = "codex-sessions"
ALLOWED_GITHUB_ORG = "e3-solutions"
EXCERPT_BYTES = 4096
PLUGIN_VERSION = "0.1.0"


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
    user_key = "local"
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
    enqueue_record(base, event)
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


def enqueue_record(base: Path, record: JsonDict) -> None:
    write_json_atomic(queue_record_path(base, record), record)


def queue_record_path(base: Path, record: JsonDict) -> Path:
    record_id = safe_segment(str(record["id"]))
    return pending_queue_dir(base) / f"{record_id}.json"


def pending_queue_paths(base: Path) -> list[Path]:
    return sorted(pending_queue_dir(base).glob("*.json"))


def pending_queue_dir(base: Path) -> Path:
    return base / "queue" / "pending"


def processing_queue_dir(base: Path) -> Path:
    return base / "queue" / "processing"


def read_json_file(path: Path) -> JsonDict:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return loaded


def migrate_legacy_queue(base: Path) -> None:
    queue_path = base / "queue.jsonl"
    records = read_jsonl(queue_path)
    for record in records:
        enqueue_record(base, record)
    if records:
        queue_path.unlink(missing_ok=True)
    for legacy_path in sorted((base / "queue").glob("*.json")):
        target = pending_queue_dir(base) / legacy_path.name
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            legacy_path.replace(target)
        except FileNotFoundError:
            continue


def recover_processing_records(base: Path) -> None:
    pending_dir = pending_queue_dir(base)
    pending_dir.mkdir(parents=True, exist_ok=True)
    for processing_path in sorted(processing_queue_dir(base).glob("*.json")):
        try:
            processing_path.replace(pending_dir / processing_path.name)
        except FileNotFoundError:
            continue


def claim_queue_path(path: Path, base: Path) -> Path | None:
    claimed_path = processing_queue_dir(base) / path.name
    claimed_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.replace(claimed_path)
    except FileNotFoundError:
        return None
    return claimed_path


@contextlib.contextmanager
def drain_lock(base: Path):
    lock_path = base / "queue" / "drain.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def drain_queue() -> JsonDict:
    base = ensure_state_dir()
    with drain_lock(base) as acquired:
        if not acquired:
            return {"uploaded": 0, "failed": 0, "remaining": len(pending_queue_paths(base)), "locked": True}

        migrate_legacy_queue(base)
        recover_processing_records(base)
        uploaded = 0
        failed = 0
        failed_record_names: set[str] = set()
        uploader: IngestUploader | None = None
        while True:
            queue_paths = [path for path in pending_queue_paths(base) if path.name not in failed_record_names]
            if not queue_paths:
                break
            if uploader is None:
                uploader = IngestUploader.from_env()
            for queue_path in queue_paths:
                claimed_path = claim_queue_path(queue_path, base)
                if claimed_path is None:
                    continue
                record: JsonDict | None = None
                try:
                    record = read_json_file(claimed_path)
                    uploader.upload_message(record, base=base)
                except Exception as exc:  # noqa: BLE001 - hook uploader must preserve queue on any failure.
                    failed += 1
                    failed_record_names.add(claimed_path.name)
                    if record is None:
                        claimed_path.replace(pending_queue_dir(base) / claimed_path.name)
                        continue
                    record["last_upload_error"] = str(exc)
                    record["last_upload_failed_at"] = now_iso()
                    enqueue_record(base, record)
                    claimed_path.unlink(missing_ok=True)
                else:
                    uploaded += 1
                    claimed_path.unlink(missing_ok=True)
        return {"uploaded": uploaded, "failed": failed, "remaining": len(pending_queue_paths(base))}


def try_auto_drain() -> None:
    if not upload_configured():
        return
    try:
        spawn_drain()
    except Exception as exc:  # noqa: BLE001 - capture must not fail because remote upload is unavailable.
        append_jsonl(ensure_state_dir() / "upload_errors.jsonl", {"created_at": now_iso(), "error": str(exc)})


def upload_configured() -> bool:
    return auto_upload_enabled() and bool(ingest_url())


def auto_upload_enabled() -> bool:
    value = os.environ.get(AUTO_UPLOAD_ENV, "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def spawn_drain() -> None:
    script = Path(__file__).with_name("drain_queue.py")
    subprocess.Popen(
        [sys.executable, str(script)],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        start_new_session=True,
    )


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


def ingest_url() -> str:
    return os.environ.get(INGEST_URL_ENV) or DEFAULT_INGEST_URL


def build_ingest_payload(record: JsonDict, *, base: Path) -> JsonDict:
    message = read_json_file(base / str(record["local_content_path"]))
    return {
        "version": 1,
        "plugin": {
            "name": "codex-session-logging",
            "version": PLUGIN_VERSION,
        },
        "record": record,
        "message": message,
        "client": client_context(record),
    }


def client_context(record: JsonDict) -> JsonDict:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    cwd = metadata.get("cwd") if isinstance(metadata.get("cwd"), str) else None
    context: JsonDict = {
        "cwd": cwd,
        "repo_remote": git_origin_remote(cwd) if cwd else None,
        "git_email": git_config_value(cwd, "user.email") if cwd else None,
        "git_user_name": git_config_value(cwd, "user.name") if cwd else None,
        "git_branch": current_git_branch(cwd) if cwd else None,
        "hostname": local_hostname(),
        "local_username": local_username(),
    }
    return {key: value for key, value in context.items() if value}


def git_config_value(cwd: str, key: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", cwd, "config", "--get", key],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def current_git_branch(cwd: str) -> str | None:
    result = subprocess.run(
        ["git", "-C", cwd, "branch", "--show-current"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        check=False,
    )
    if result.returncode != 0:
        return None
    value = result.stdout.strip()
    return value or None


def local_hostname() -> str | None:
    try:
        return socket.gethostname() or None
    except OSError:
        return None


def local_username() -> str | None:
    try:
        return getpass.getuser() or None
    except (KeyError, OSError):
        return None


class IngestUploader:
    def __init__(self, *, url: str, token: str | None = None) -> None:
        self.url = url
        self.token = token

    @classmethod
    def from_env(cls) -> "IngestUploader":
        return cls(url=ingest_url(), token=os.environ.get(INGEST_TOKEN_ENV))

    def upload_message(self, record: JsonDict, *, base: Path) -> None:
        self.post(build_ingest_payload(record, base=base))

    def post(self, payload: JsonDict) -> None:
        headers = {
            "content-type": "application/json",
            "user-agent": f"codex-session-logging/{PLUGIN_VERSION}",
        }
        if self.token:
            headers["x-codex-session-log-token"] = self.token
        self.request(
            self.url,
            method="POST",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
        )

    def request(self, url: str, *, method: str, data: bytes, headers: dict[str, str]) -> bytes:
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"Codex session ingest failed {exc.code}: {body}") from exc


if __name__ == "__main__":
    main()
