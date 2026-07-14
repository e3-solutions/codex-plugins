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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback.
    tomllib = None  # type: ignore[assignment]

JsonDict = dict[str, Any]

STATE_DIR_ENV = "CODEX_SESSION_LOG_STATE_DIR"
SUPABASE_BUCKET_ENV = "CODEX_SESSION_LOG_BUCKET"
INGEST_URL_ENV = "CODEX_SESSION_LOG_INGEST_URL"
INGEST_TOKEN_ENV = "CODEX_SESSION_LOG_INGEST_TOKEN"
AUTO_UPLOAD_ENV = "CODEX_SESSION_LOG_AUTO_UPLOAD"
UPLOAD_WORKERS_ENV = "CODEX_SESSION_LOG_UPLOAD_WORKERS"
DEFAULT_SUPABASE_URL = "https://pmdfllwuctzkdjiehezq.supabase.co"
DEFAULT_INGEST_URL = f"{DEFAULT_SUPABASE_URL}/functions/v1/codex-session-ingest"
DEFAULT_BUCKET = "codex-sessions"
ALLOWED_GITHUB_ORG = "e3-solutions"
EXCERPT_BYTES = 4096
PLUGIN_VERSION = "0.2.2"
PERMANENT_HTTP_STATUSES = {400, 413, 415, 422}


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
        summary = {
            "captured": captured["id"],
            "type": captured.get("type", "message"),
        }
        for key in ("role", "event_type"):
            if captured.get(key):
                summary[key] = captured[key]
        print(json.dumps(summary, sort_keys=True))


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
    if not should_capture_payload(payload):
        return None
    if role and content is not None:
        return capture_message_event(payload, hook_event=hook_event, role=role, content=content)
    event_type, event_metadata = event_from_payload(hook_event, payload)
    if not event_type:
        return None
    return capture_metadata_event(payload, hook_event=hook_event, event_type=event_type, event_metadata=event_metadata)


def capture_message_event(payload: JsonDict, *, hook_event: str, role: str, content: str) -> JsonDict:
    base = ensure_state_dir()
    session_id = safe_segment(first_string(payload, "session_id", "sessionId") or "unknown-session")
    thread_id = thread_id_from_payload(payload)
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
    if thread_id:
        message["thread_id"] = thread_id
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
    if thread_id:
        event["thread_id"] = thread_id
    append_jsonl(base / "events.jsonl", event)
    enqueue_record(base, event)
    try_auto_drain()
    return event


def capture_metadata_event(
    payload: JsonDict,
    *,
    hook_event: str,
    event_type: str,
    event_metadata: JsonDict,
) -> JsonDict:
    base = ensure_state_dir()
    session_id = safe_segment(first_string(payload, "session_id", "sessionId") or "unknown-session")
    thread_id = thread_id_from_payload(payload)
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
    if thread_id:
        detail["thread_id"] = thread_id
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
    if thread_id:
        event["thread_id"] = thread_id
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


def event_from_payload(hook_event: str, payload: JsonDict) -> tuple[str | None, JsonDict]:
    if hook_event == "SessionStart":
        return "environment_snapshot", {"codex_setup": codex_setup_snapshot()}
    if hook_event == "PreToolUse":
        metadata = tool_event_metadata(payload, phase="started")
        return ("tool_call_started", metadata) if metadata.get("tool_name") else (None, {})
    if hook_event == "PostToolUse":
        metadata = tool_event_metadata(payload, phase="finished")
        success = tool_success(payload)
        if success is not None:
            metadata["success"] = success
        return ("tool_call_finished", metadata) if metadata.get("tool_name") else (None, {})
    return None, {}


def metadata_from_payload(payload: JsonDict) -> JsonDict:
    metadata: JsonDict = {}
    cwd = first_string(payload, "cwd") or os.getcwd()
    if cwd:
        metadata["cwd"] = cwd
    for key in ("transcript_path", "model", "source"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            metadata[key] = value
    return metadata


def thread_id_from_payload(payload: JsonDict) -> str | None:
    transcript_path = first_string(payload, "transcript_path", "transcriptPath")
    return sha256_hex(transcript_path) if transcript_path else None


def sha256_hex(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


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


def codex_setup_snapshot() -> JsonDict:
    config = read_codex_config()
    snapshot: JsonDict = {
        "settings": codex_settings(config),
        "plugins": codex_plugins(config),
        "skills": codex_skills(),
        "mcp_servers": codex_mcp_servers(config),
        "marketplaces": codex_marketplaces(config),
        "apps": codex_apps(config),
        "connections": codex_connections(config),
    }
    return {key: value for key, value in snapshot.items() if value not in ({}, [])}


def codex_config_path() -> Path:
    home = os.environ.get("CODEX_HOME")
    base = Path(home).expanduser().resolve() if home else Path.home() / ".codex"
    return base / "config.toml"


def read_codex_config() -> JsonDict:
    path = codex_config_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    if tomllib is not None:
        try:
            data = tomllib.loads(raw)
        except Exception:
            return fallback_codex_config(raw)
        return data if isinstance(data, dict) else {}
    return fallback_codex_config(raw)


def fallback_codex_config(raw: str) -> JsonDict:
    config: JsonDict = {
        "plugins": {},
        "mcp_servers": {},
        "marketplaces": {},
        "apps": {},
    }
    for match in re.finditer(r'^\[plugins\."([^"]+)"\]', raw, flags=re.MULTILINE):
        config["plugins"][match.group(1)] = {"enabled": True}
    for match in re.finditer(r"^\[mcp_servers\.([^\]\.]+)\]", raw, flags=re.MULTILINE):
        config["mcp_servers"][match.group(1).strip('"')] = {}
    for match in re.finditer(r"^\[marketplaces\.([^\]]+)\]", raw, flags=re.MULTILINE):
        config["marketplaces"][match.group(1).strip('"')] = {}
    for key in ("model", "service_tier", "sandbox_mode", "personality"):
        match = re.search(rf'^{key}\s*=\s*"([^"]*)"', raw, flags=re.MULTILINE)
        if match:
            config[key] = match.group(1)
    return config


def codex_settings(config: JsonDict) -> JsonDict:
    allowed = (
        "model",
        "model_reasoning_effort",
        "plan_mode_reasoning_effort",
        "service_tier",
        "sandbox_mode",
        "personality",
    )
    return {
        key: value
        for key in allowed
        if isinstance((value := config.get(key)), str) and value
    }


def codex_plugins(config: JsonDict) -> list[JsonDict]:
    plugins = config.get("plugins")
    if not isinstance(plugins, dict):
        return []
    result: list[JsonDict] = []
    for name, value in plugins.items():
        if not isinstance(name, str):
            continue
        enabled = value.get("enabled") if isinstance(value, dict) else None
        result.append({"name": name, "enabled": enabled is not False})
    return sorted(result, key=lambda item: str(item["name"]))


def codex_skills() -> list[JsonDict]:
    result: list[JsonDict] = []
    seen: set[tuple[str, str, str, str]] = set()
    for skill_file in iter_skill_files():
        item = skill_item_for_path(skill_file)
        if not item:
            continue
        key = (
            str(item.get("source") or ""),
            str(item.get("marketplace") or ""),
            str(item.get("plugin") or ""),
            str(item.get("name") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return sorted(result, key=lambda item: json.dumps(item, sort_keys=True))


def iter_skill_files() -> list[Path]:
    roots = [
        codex_config_path().parent / "skills",
        agents_home_path() / "skills",
        codex_config_path().parent / "plugins" / "cache",
    ]
    files: list[Path] = []
    for root in roots:
        try:
            files.extend(path for path in root.rglob("SKILL.md") if path.is_file())
        except OSError:
            continue
    return files


def agents_home_path() -> Path:
    home = os.environ.get("AGENTS_HOME")
    return Path(home).expanduser().resolve() if home else Path.home() / ".agents"


def skill_item_for_path(path: Path) -> JsonDict | None:
    codex_home = codex_config_path().parent
    agents_home = agents_home_path()
    for base, source in ((codex_home / "skills", "user"), (agents_home / "skills", "agent")):
        try:
            rel = path.relative_to(base)
        except ValueError:
            continue
        parts = rel.parts
        if not parts:
            return None
        if parts[0] == ".system" and len(parts) > 1:
            return {"name": parts[1], "source": "system"}
        return {"name": parts[0], "source": source}

    cache_root = codex_home / "plugins" / "cache"
    try:
        rel = path.relative_to(cache_root)
    except ValueError:
        return None
    parts = rel.parts
    if "skills" not in parts:
        return None
    skills_index = parts.index("skills")
    if skills_index + 1 >= len(parts):
        return None
    item: JsonDict = {
        "name": parts[skills_index + 1],
        "source": "plugin",
    }
    if len(parts) > 0:
        item["marketplace"] = parts[0]
    if len(parts) > 1:
        item["plugin"] = parts[1]
    if len(parts) > 2 and skills_index >= 3:
        item["version"] = parts[2]
    return item


def codex_mcp_servers(config: JsonDict) -> list[JsonDict]:
    servers = config.get("mcp_servers")
    if not isinstance(servers, dict):
        return []
    result: list[JsonDict] = []
    for name, value in servers.items():
        if not isinstance(name, str) or not isinstance(value, dict):
            continue
        transport = "url" if isinstance(value.get("url"), str) else "command" if isinstance(value.get("command"), str) else "unknown"
        result.append({"name": name, "transport": transport})
    return sorted(result, key=lambda item: str(item["name"]))


def codex_marketplaces(config: JsonDict) -> list[JsonDict]:
    marketplaces = config.get("marketplaces")
    if not isinstance(marketplaces, dict):
        return []
    result: list[JsonDict] = []
    for name, value in marketplaces.items():
        if not isinstance(name, str):
            continue
        item: JsonDict = {"name": name}
        if isinstance(value, dict) and isinstance(value.get("source_type"), str):
            item["source_type"] = value["source_type"]
        result.append(item)
    return sorted(result, key=lambda item: str(item["name"]))


def codex_apps(config: JsonDict) -> list[JsonDict]:
    apps = config.get("apps")
    if not isinstance(apps, dict):
        return []
    return [{"id": name} for name in sorted(name for name in apps if isinstance(name, str))]


def codex_connections(config: JsonDict) -> list[JsonDict]:
    apps = config.get("apps")
    if not isinstance(apps, dict):
        return []
    result: list[JsonDict] = []
    for app_id, value in apps.items():
        if not isinstance(app_id, str):
            continue
        item: JsonDict = {"id": app_id}
        if isinstance(value, dict) and isinstance(value.get("tools"), dict):
            tools = sorted(name for name in value["tools"] if isinstance(name, str))
            if tools:
                item["tools"] = tools
        result.append(item)
    return sorted(result, key=lambda item: str(item["id"]))


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


def dead_letter_queue_dir(base: Path) -> Path:
    return base / "queue" / "dead-letter"


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


def drain_queue(progress_callback: Callable[[JsonDict], None] | None = None) -> JsonDict:
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

        migrate_legacy_queue(base)
        recover_processing_records(base)
        uploaded = 0
        failed = 0
        dead_lettered = 0
        failed_record_names: set[str] = set()
        uploader: IngestUploader | None = None
        with ThreadPoolExecutor(max_workers=upload_worker_count()) as executor:
            while True:
                queue_paths = [path for path in pending_queue_paths(base) if path.name not in failed_record_names]
                if not queue_paths:
                    break
                if uploader is None:
                    uploader = IngestUploader.from_env()
                claimed_paths = [
                    claimed
                    for path in queue_paths
                    if (claimed := claim_queue_path(path, base)) is not None
                ]
                for status, record_name in executor.map(
                    lambda path: upload_claimed_record(path, uploader=uploader, base=base),
                    claimed_paths,
                ):
                    if status == "uploaded":
                        uploaded += 1
                    elif status == "dead_lettered":
                        dead_lettered += 1
                    else:
                        failed += 1
                        failed_record_names.add(record_name)
                    if progress_callback:
                        progress_callback({
                            "uploaded": uploaded,
                            "failed": failed,
                            "dead_lettered": dead_lettered,
                            "remaining": len(pending_queue_paths(base))
                            + len(list(processing_queue_dir(base).glob("*.json"))),
                        })
        return {
            "uploaded": uploaded,
            "failed": failed,
            "dead_lettered": dead_lettered,
            "remaining": len(pending_queue_paths(base)),
        }


def upload_worker_count() -> int:
    try:
        return min(32, max(1, int(os.environ.get(UPLOAD_WORKERS_ENV, "4"))))
    except ValueError:
        return 4


def upload_claimed_record(
    claimed_path: Path,
    *,
    uploader: "IngestUploader",
    base: Path,
) -> tuple[str, str]:
    record: JsonDict | None = None
    try:
        record = read_json_file(claimed_path)
        uploader.upload_message(record, base=base)
    except PermanentUploadError as exc:
        if record is None:
            target = dead_letter_queue_dir(base) / claimed_path.name
            target.parent.mkdir(parents=True, exist_ok=True)
            claimed_path.replace(target)
        else:
            dead_letter_record(base, record, claimed_path, exc)
        return "dead_lettered", claimed_path.name
    except Exception as exc:  # noqa: BLE001 - uploader must preserve queue on any failure.
        if record is None:
            claimed_path.replace(pending_queue_dir(base) / claimed_path.name)
        else:
            record["last_upload_error"] = str(exc)
            record["last_upload_failed_at"] = now_iso()
            enqueue_record(base, record)
            claimed_path.unlink(missing_ok=True)
        return "failed", claimed_path.name
    (dead_letter_queue_dir(base) / claimed_path.name).unlink(missing_ok=True)
    claimed_path.unlink(missing_ok=True)
    return "uploaded", claimed_path.name


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
    detail = read_json_file(base / str(record["local_content_path"]))
    payload = {
        "version": 1,
        "plugin": {
            "name": "codex-session-logging",
            "version": PLUGIN_VERSION,
        },
        "record": record,
        "client": client_context(record, base=base),
    }
    if record.get("type") == "event":
        payload["event"] = detail
    elif record.get("type") == "usage":
        payload["usage"] = detail
    else:
        payload["message"] = detail
    return payload


def client_context(record: JsonDict, *, base: Path) -> JsonDict:
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    cwd = metadata.get("cwd") if isinstance(metadata.get("cwd"), str) else None
    git_email = git_config_value(cwd, "user.email")
    hostname = local_hostname()
    username = local_username()
    installation_id = local_installation_id(base)
    context: JsonDict = {
        "cwd": cwd,
        "repo_remote": metadata.get("repo_remote")
        or (git_origin_remote(cwd) if cwd else None),
        "git_email": git_email,
        "git_user_name": git_config_value(cwd, "user.name"),
        "git_branch": metadata.get("git_branch")
        or (current_git_branch(cwd) if cwd else None),
        "linear_user_name": saved_linear_user_name(),
        "hostname": hostname,
        "local_username": username,
        "installation_id": installation_id,
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

    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = path.with_suffix(".lock")
        with lock_path.open("a", encoding="utf-8") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            try:
                try:
                    existing = path.read_text(encoding="utf-8").strip()
                except (FileNotFoundError, OSError):
                    existing = ""
                if existing:
                    return existing

                value = str(uuid.uuid4())
                tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
                tmp.write_text(f"{value}\n", encoding="utf-8")
                tmp.replace(path)
                return value
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)
    except OSError:
        return str(uuid.uuid4())


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
    path = codex_config_path().parent / "linear-sync" / "user.json"
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


def git_config_value(cwd: str | None, key: str) -> str | None:
    commands = []
    if cwd:
        commands.append(["git", "-C", cwd, "config", "--get", key])
    commands.append(["git", "config", "--global", "--get", key])
    for command in commands:
        result = subprocess.run(
            command,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return None


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
        payload = build_ingest_payload(record, base=base)
        validate_ingest_payload(payload)
        self.post(payload)

    def post(self, payload: JsonDict) -> None:
        headers = {
            "content-type": "application/json",
            "connection": "close",
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
            with urllib.request.urlopen(request, timeout=30):
                return b""
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            message = f"Codex session ingest failed {exc.code}: {body}"
            if exc.code in PERMANENT_HTTP_STATUSES:
                raise PermanentUploadError(message, status=exc.code) from exc
            raise RuntimeError(message) from exc


class PermanentUploadError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


def validate_ingest_payload(payload: JsonDict) -> None:
    client = payload.get("client")
    if not isinstance(client, dict) or not non_empty_string(client.get("repo_remote")):
        raise PermanentUploadError("client.repo_remote must be a non-empty string")


def non_empty_string(value: object) -> bool:
    return isinstance(value, str) and len(value) > 0


if __name__ == "__main__":
    main()
