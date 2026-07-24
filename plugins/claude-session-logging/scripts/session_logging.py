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

STATE_DIR_ENV = "CLAUDE_SESSION_LOG_STATE_DIR"
SUPABASE_BUCKET_ENV = "CLAUDE_SESSION_LOG_BUCKET"
INGEST_URL_ENV = "CLAUDE_SESSION_LOG_INGEST_URL"
INGEST_TOKEN_ENV = "CLAUDE_SESSION_LOG_INGEST_TOKEN"
AUTO_UPLOAD_ENV = "CLAUDE_SESSION_LOG_AUTO_UPLOAD"
ALLOWED_GITHUB_ORG_ENV = "CLAUDE_SESSION_LOG_ALLOWED_GITHUB_ORG"
DEFAULT_SUPABASE_URL = "https://pmdfllwuctzkdjiehezq.supabase.co"
DEFAULT_INGEST_URL = f"{DEFAULT_SUPABASE_URL}/functions/v1/codex-session-ingest"
DEFAULT_BUCKET = "codex-sessions"
DEFAULT_ALLOWED_GITHUB_ORG = "e3-solutions"
PLUGIN_NAME = "claude-session-logging"
PLUGIN_VERSION = "git"
PLATFORM = "claude-code"
# Coding-agent family stamped on every payload so the ingest, heartbeat
# dashboard, and codestat can label/classify Claude sessions. Codex uses "codex".
AGENT = "claude"
# Event types that mark the end of a session. These carry ``ended_at`` so the
# ingest can set codex_sessions.ended_at; every other event clears it (see the
# ingest upsertSession branch).
END_EVENT_TYPES = frozenset({"thread_stopped", "thread_ended"})
PERMANENT_HTTP_STATUSES = {400, 403, 413, 415, 422}
# Byte budget for the utf8 excerpt stored alongside full-body message records
# (matches codex-session-logging so both agents produce identical shapes).
EXCERPT_BYTES = 4096


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Capture and upload Claude Code thread/tool events.")
    subparsers = parser.add_subparsers(dest="command")
    capture_parser = subparsers.add_parser("capture", help="Capture one hook payload from stdin.")
    capture_parser.add_argument("--event", help="Hook event name override.")
    subparsers.add_parser("drain", help="Upload queued records to Supabase.")
    args = parser.parse_args(argv)

    if args.command == "drain":
        print(json.dumps(drain_queue(), sort_keys=True))
        return

    captured = capture_hook_event(read_stdin_json(), event_name=getattr(args, "event", None))
    if captured:
        print(json.dumps({"captured": captured["id"], "type": "event", "event_type": captured["event_type"]}, sort_keys=True))


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
    if not should_capture_payload(payload):
        return None
    event_type, event_metadata = event_from_payload(hook_event, payload)
    if not event_type:
        return None
    return capture_metadata_event(payload, hook_event=hook_event, event_type=event_type, event_metadata=event_metadata)


def event_from_payload(hook_event: str, payload: JsonDict) -> tuple[str | None, JsonDict]:
    if hook_event == "SessionStart":
        return "thread_started", {"thread_event": "started"}
    if hook_event == "UserPromptSubmit":
        metadata: JsonDict = {"thread_event": "prompt_submitted"}
        prompt = first_string(payload, "prompt")
        if prompt is not None:
            prompt_bytes = prompt.encode("utf-8")
            metadata["prompt_sha256"] = sha256_hex(prompt)
            metadata["prompt_byte_size"] = len(prompt_bytes)
        return "thread_prompt_submitted", metadata
    if hook_event == "Stop":
        metadata = {"thread_event": "stopped"}
        copy_bool(payload, metadata, "stop_hook_active")
        return "thread_stopped", metadata
    if hook_event == "StopFailure":
        metadata = {"thread_event": "stop_failed"}
        copy_string(payload, metadata, "error_type")
        copy_string(payload, metadata, "reason", destination="stop_reason")
        return "thread_stop_failed", metadata
    if hook_event == "PreCompact":
        metadata = {"thread_event": "compaction_started"}
        copy_string(payload, metadata, "trigger", destination="compaction_trigger")
        return "thread_compaction_started", metadata
    if hook_event == "PostCompact":
        metadata = {"thread_event": "compaction_finished"}
        copy_string(payload, metadata, "trigger", destination="compaction_trigger")
        return "thread_compaction_finished", metadata
    if hook_event == "SessionEnd":
        metadata = {"thread_event": "ended"}
        copy_string(payload, metadata, "reason", destination="session_end_reason")
        return "thread_ended", metadata
    if hook_event == "PreToolUse":
        metadata = tool_event_metadata(payload, phase="started")
        return ("tool_call_started", metadata) if metadata.get("tool_name") else (None, {})
    if hook_event == "PostToolUse":
        metadata = tool_event_metadata(payload, phase="finished")
        success = tool_success(payload)
        if success is not None:
            metadata["success"] = success
        return ("tool_call_finished", metadata) if metadata.get("tool_name") else (None, {})
    if hook_event == "PostToolUseFailure":
        metadata = tool_event_metadata(payload, phase="failed")
        metadata["success"] = False
        return ("tool_call_failed", metadata) if metadata.get("tool_name") else (None, {})
    if hook_event == "PostToolBatch":
        metadata = {"thread_event": "tool_batch_finished"}
        batch_size = tool_batch_size(payload)
        if batch_size is not None:
            metadata["tool_batch_size"] = batch_size
        return "tool_batch_finished", metadata
    if hook_event == "PermissionRequest":
        metadata = tool_event_metadata(payload, phase="permission_requested")
        return ("tool_permission_requested", metadata) if metadata.get("tool_name") else (None, {})
    if hook_event == "PermissionDenied":
        metadata = tool_event_metadata(payload, phase="permission_denied")
        return ("tool_permission_denied", metadata) if metadata.get("tool_name") else (None, {})
    return None, {}


def capture_metadata_event(
    payload: JsonDict,
    *,
    hook_event: str,
    event_type: str,
    event_metadata: JsonDict,
) -> JsonDict:
    base = ensure_state_dir()
    session_id = safe_segment(first_string(payload, "session_id", "sessionId") or "unknown-session")
    turn_id = first_string(payload, "turn_id", "turnId")
    seq = next_sequence(base, session_id)
    created_at = now_iso()
    metadata = metadata_from_payload(payload)
    metadata.update(event_metadata)
    event_id = uuid.uuid4().hex
    storage_path = f"users/local/sessions/{session_id}/events/{seq:06d}-{safe_segment(event_type)}.json"
    detail = {
        "id": event_id,
        "session_id": session_id,
        "turn_id": turn_id,
        "seq": seq,
        "event_type": event_type,
        "hook_event_name": hook_event,
        "created_at": created_at,
        "metadata": metadata,
    }
    write_json_atomic(base / storage_path, detail)

    event = {
        "id": event_id,
        "type": "event",
        "session_id": session_id,
        "turn_id": turn_id,
        "seq": seq,
        "event_type": event_type,
        "hook_event_name": hook_event,
        "storage_bucket": bucket_name(),
        "storage_path": storage_path,
        "local_content_path": storage_path,
        "metadata": metadata,
        "created_at": created_at,
        "uploaded_at": None,
    }
    if event_type in END_EVENT_TYPES:
        # Stop / SessionEnd end the session; the ingest persists this as
        # codex_sessions.ended_at. A later prompt, tool call, or presence tick
        # clears it again, so a resumed session lights back up.
        event["ended_at"] = created_at
    append_jsonl(base / "events.jsonl", event)
    enqueue_record(base, event)
    try_auto_drain()
    return event


def metadata_from_payload(payload: JsonDict) -> JsonDict:
    metadata: JsonDict = {"platform": PLATFORM, "agent": AGENT}
    cwd = first_string(payload, "cwd") or os.getcwd()
    if cwd:
        metadata["cwd"] = cwd
    for key in ("transcript_path", "model", "source", "permission_mode"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            metadata[key] = value
    return metadata


def tool_event_metadata(payload: JsonDict, *, phase: str) -> JsonDict:
    tool = tool_name(payload)
    if not tool:
        return {}
    metadata: JsonDict = {
        "tool_name": tool,
        "tool_phase": phase,
    }
    call_id = first_string(payload, "tool_call_id", "toolCallId", "call_id", "callId")
    if call_id:
        metadata["tool_call_id"] = call_id
    return metadata


def tool_name(payload: JsonDict) -> str:
    for key in ("tool_name", "toolName", "name", "tool"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    tool = payload.get("tool")
    if isinstance(tool, dict) and isinstance(tool.get("name"), str):
        return tool["name"].strip()
    return ""


def tool_success(payload: JsonDict) -> bool | None:
    for key in ("success", "succeeded", "ok"):
        value = payload.get(key)
        if isinstance(value, bool):
            return value
    status = first_string(payload, "status", "result")
    if status:
        normalized = status.strip().lower()
        if normalized in {"success", "succeeded", "ok", "passed", "complete", "completed"}:
            return True
        if normalized in {"failure", "failed", "error", "errored"}:
            return False
    return None


def tool_batch_size(payload: JsonDict) -> int | None:
    for key in ("tool_results", "tools", "results"):
        value = payload.get(key)
        if isinstance(value, list):
            return len(value)
    return None


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def utf8_excerpt(content_bytes: bytes, *, limit: int = EXCERPT_BYTES) -> str:
    return content_bytes[:limit].decode("utf-8", errors="replace")


def deterministic_uuid(value: str) -> str:
    """Stable v5-ish uuid from a seed string so re-emits carry the same id and
    dedupe via the ingest's ``on_conflict=id`` (mirrors backfill_sessions)."""
    digest = bytearray(hashlib.sha256(value.encode("utf-8")).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x50
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


def copy_string(source: JsonDict, target: JsonDict, key: str, *, destination: str | None = None) -> None:
    destination = destination or key
    value = source.get(key)
    if isinstance(value, str) and value:
        target[destination] = value


def copy_bool(source: JsonDict, target: JsonDict, key: str) -> None:
    value = source.get(key)
    if isinstance(value, bool):
        target[key] = value


def should_capture_payload(payload: JsonDict) -> bool:
    cwd = first_string(payload, "cwd") or os.getcwd()
    remote = git_origin_remote(cwd)
    return remote_belongs_to_org(remote, allowed_github_org())


def allowed_github_org() -> str:
    return os.environ.get(ALLOWED_GITHUB_ORG_ENV) or DEFAULT_ALLOWED_GITHUB_ORG


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
    return Path.home() / ".claude" / "session-logging"


def next_sequence(base: Path, session_id: str) -> int:
    path = base / "sessions" / session_id / "sequence.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    with lock_path.open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            try:
                current = int(path.read_text(encoding="utf-8").strip() or "0")
            except (FileNotFoundError, OSError, ValueError):
                current = 0
            value = current + 1
            path.write_text(f"{value}\n", encoding="utf-8")
            return value
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


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
    return pending_queue_dir(base) / f"{safe_segment(str(record['id']))}.json"


def pending_queue_paths(base: Path) -> list[Path]:
    return sorted(pending_queue_dir(base).glob("*.json"))


def pending_queue_dir(base: Path) -> Path:
    return base / "queue" / "pending"


def processing_queue_dir(base: Path) -> Path:
    return base / "queue" / "processing"


def dead_letter_queue_dir(base: Path) -> Path:
    return base / "queue" / "dead-letter"


def read_json_file(path: Path) -> JsonDict:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(loaded, dict):
        raise ValueError(f"{path} does not contain a JSON object")
    return loaded


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
            return {
                "uploaded": 0,
                "failed": 0,
                "dead_lettered": 0,
                "remaining": len(pending_queue_paths(base)),
                "locked": True,
            }

        recover_processing_records(base)
        uploaded = 0
        failed = 0
        dead_lettered = 0
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
                    uploader.upload_record(record, base=base)
                except PermanentUploadError as exc:
                    dead_lettered += 1
                    if record is None:
                        target = dead_letter_queue_dir(base) / claimed_path.name
                        target.parent.mkdir(parents=True, exist_ok=True)
                        claimed_path.replace(target)
                        continue
                    dead_letter_record(base, record, claimed_path, exc)
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
        return {
            "uploaded": uploaded,
            "failed": failed,
            "dead_lettered": dead_lettered,
            "remaining": len(pending_queue_paths(base)),
        }


def dead_letter_record(base: Path, record: JsonDict, source_path: Path, exc: Exception) -> None:
    failed_at = now_iso()
    record["last_upload_error"] = str(exc)
    record["last_upload_failed_at"] = failed_at
    record["dead_letter_reason"] = "permanent_upload_failure"
    record["dead_lettered_at"] = failed_at
    write_json_atomic(dead_letter_queue_dir(base) / source_path.name, record)
    source_path.unlink(missing_ok=True)


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


def ingest_url() -> str:
    return os.environ.get(INGEST_URL_ENV) or DEFAULT_INGEST_URL


def build_ingest_payload(record: JsonDict, *, base: Path) -> JsonDict:
    detail = read_json_file(base / str(record["local_content_path"]))
    payload: JsonDict = {
        "version": 1,
        "plugin": {
            "name": PLUGIN_NAME,
            "version": PLUGIN_VERSION,
        },
        "record": record,
        "client": client_context(record, base=base),
    }
    record_type = record.get("type")
    if record_type == "message":
        # Full prompt/response/tool bodies (FULL CODEX PARITY): the ingest
        # verifies content_sha256/byte_size then stores + upserts codex_session_messages.
        payload["message"] = detail
    elif record_type == "usage":
        # Per-turn token usage -> codex_session_usage (feeds codestat token_usage_by_agent).
        payload["usage"] = detail
    else:
        payload["event"] = detail
    return payload


def client_context(record: JsonDict, *, base: Path) -> JsonDict:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    cwd = metadata.get("cwd") if isinstance(metadata.get("cwd"), str) else None
    git_email = git_config_value(cwd, "user.email") if cwd else None
    hostname = local_hostname()
    username = local_username()
    installation_id = local_installation_id(base)
    context: JsonDict = {
        "cwd": cwd,
        "repo_remote": git_origin_remote(cwd) if cwd else None,
        "git_email": git_email,
        "git_user_name": git_config_value(cwd, "user.name") if cwd else None,
        "git_branch": current_git_branch(cwd) if cwd else None,
        "linear_user_name": saved_linear_user_name(),
        "hostname": hostname,
        "local_username": username,
        "installation_id": installation_id,
        "platform": PLATFORM,
    }
    identity_key = client_identity_key(
        git_email=git_email,
        installation_id=installation_id,
        local_username=username,
        hostname=hostname,
    )
    if identity_key:
        context["identity_key"] = identity_key
    return {key: value for key, value in context.items() if value}


def local_installation_id(base: Path) -> str:
    path = base / "installation_id"
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except (FileNotFoundError, OSError):
        existing = ""
    if existing:
        return existing

    value = str(uuid.uuid4())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(f"{value}\n", encoding="utf-8")
        tmp.replace(path)
    except OSError:
        return value
    return value


def client_identity_key(
    *,
    git_email: str | None,
    installation_id: str | None,
    local_username: str | None,
    hostname: str | None,
) -> str | None:
    if git_email:
        return f"git_email:{git_email.strip().lower()}"
    if installation_id:
        return f"installation:{installation_id.strip()}"
    if local_username and hostname:
        return f"local:{local_username.strip()}@{hostname.strip()}"
    if local_username:
        return f"local_username:{local_username.strip()}"
    if hostname:
        return f"hostname:{hostname.strip()}"
    return None


def saved_linear_user_name() -> str | None:
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))).expanduser()
    path = codex_home / "linear-sync" / "user.json"
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(loaded, dict):
        return None
    value = loaded.get("linear_name")
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped or None


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

    def upload_record(self, record: JsonDict, *, base: Path) -> None:
        payload = build_ingest_payload(record, base=base)
        validate_ingest_payload(payload)
        self.post(payload)

    def post(self, payload: JsonDict) -> None:
        headers = {
            "content-type": "application/json",
            "connection": "close",
            "user-agent": f"{PLUGIN_NAME}/{PLUGIN_VERSION}",
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
            with urllib.request.urlopen(request, timeout=30):
                return b""
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            message = f"Claude session ingest failed {exc.code}: {body}"
            if exc.code in PERMANENT_HTTP_STATUSES:
                raise PermanentUploadError(message) from exc
            raise RuntimeError(message) from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Claude session ingest failed: {exc}") from exc


def validate_ingest_payload(payload: JsonDict) -> None:
    client = payload.get("client") if isinstance(payload.get("client"), dict) else {}
    remote = client.get("repo_remote")
    if not isinstance(remote, str) or not remote.strip():
        raise PermanentUploadError("client.repo_remote must be a non-empty string")
    if not remote_belongs_to_org(remote, allowed_github_org()):
        raise PermanentUploadError("client.repo_remote is outside the allowed GitHub org")
    record = payload.get("record") if isinstance(payload.get("record"), dict) else {}
    record_type = record.get("type")
    if record_type == "event" or record_type is None:
        return
    if record_type == "message":
        _validate_message_record(record)
        return
    if record_type == "usage":
        _validate_usage_payload(payload)
        return
    raise PermanentUploadError(f"Claude session logging cannot upload record type {record_type!r}")


def _validate_message_record(record: JsonDict) -> None:
    if not isinstance(record.get("content_sha256"), str) or not record["content_sha256"]:
        raise PermanentUploadError("message record requires content_sha256")
    if not isinstance(record.get("content_byte_size"), int) or record["content_byte_size"] < 0:
        raise PermanentUploadError("message record requires a non-negative content_byte_size")
    if not isinstance(record.get("role"), str) or not record["role"]:
        raise PermanentUploadError("message record requires role")
    if not isinstance(record.get("storage_path"), str) or not record["storage_path"]:
        raise PermanentUploadError("message record requires storage_path")


def _validate_usage_payload(payload: JsonDict) -> None:
    usage = payload.get("usage") if isinstance(payload.get("usage"), dict) else None
    if usage is None:
        raise PermanentUploadError("usage record requires a usage detail")
    required_ints = (
        "input_tokens",
        "cached_input_tokens",
        "output_tokens",
        "reasoning_output_tokens",
        "total_tokens",
    )
    for key in required_ints:
        value = usage.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or value < 0:
            raise PermanentUploadError(f"usage.{key} must be a non-negative integer")
    if not isinstance(usage.get("created_at"), str) or not usage["created_at"]:
        raise PermanentUploadError("usage.created_at must be a non-empty string")


class PermanentUploadError(RuntimeError):
    pass


if __name__ == "__main__":
    main()
