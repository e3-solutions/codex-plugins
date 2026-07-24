#!/usr/bin/env python3
from __future__ import annotations

import base64
import contextlib
import fcntl
import hashlib
import json
import os
import re
import sqlite3
import time
from pathlib import Path
from typing import Any
from uuid import UUID

import publish_presence
import session_logging


JsonDict = dict[str, Any]
STATE_VERSION = 1
MAX_CHUNK_BYTES = 512 * 1024
FINGERPRINT_BYTES = 4096
DISCOVERY_PAGE_SIZE = 500
INITIAL_LOOKBACK_SECONDS_ENV = "CODEX_SESSION_ROLLOUT_INITIAL_LOOKBACK_SECONDS"
DEFAULT_INITIAL_LOOKBACK_SECONDS = 24 * 60 * 60
MAX_SYNC_BYTES_PER_HOOK = 8 * 1024 * 1024
MAX_FILES_PER_HOOK = 32
MAX_PENDING_CHECKS_PER_HOOK = 200
SAFE_CATEGORY_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
COORDINATION_TOOL_NAMES = {
    "spawn_agent",
    "wait_agent",
    "followup_task",
    "send_message",
    "send_message_to_agent",
    "interrupt_agent",
    "list_agents",
    "wait",
}


def sync_after_hook(payload: JsonDict, *, event_name: str) -> JsonDict:
    if event_name == "PostToolUse" and not is_coordination_tool(session_logging.tool_name(payload)):
        return {"queued": 0, "skipped": "ordinary_tool"}
    if event_name not in {"SessionStart", "UserPromptSubmit", "Stop", "PostToolUse"}:
        return {"queued": 0, "skipped": "unsupported_hook"}
    return sync_rollouts(hook_payload=payload)


def is_coordination_tool(name: str) -> bool:
    normalized = name.strip().lower().replace("-", "_")
    leaf = normalized.rsplit(".", 1)[-1]
    return leaf in COORDINATION_TOOL_NAMES


def sync_rollouts(
    *,
    codex_home: str | Path | None = None,
    hook_payload: JsonDict | None = None,
) -> JsonDict:
    base = session_logging.ensure_state_dir()
    with sync_lock(base) as acquired:
        if not acquired:
            return {"queued": 0, "locked": True}

        state = read_state(base)
        files = state.setdefault("files", {})
        rows, errors, databases = discover_rollout_threads(
            codex_home,
            state.get("databases") if isinstance(state.get("databases"), dict) else {},
        )
        pending_rows = state.setdefault("pending_rows", {})
        for row in rows:
            session_id = canonical_uuid(str(row.get("id") or ""))
            if session_id:
                pending_rows[session_id] = row
        pending_ordered = sorted(
            (row for row in pending_rows.values() if isinstance(row, dict)),
            key=lambda row: (int(row.get("updated_at_ms") or 0), str(row.get("id") or "")),
            reverse=True,
        )
        pending_candidates, state["pending_cursor"] = rotate_items(
            pending_ordered,
            int(state.get("pending_cursor") or 0),
            MAX_PENDING_CHECKS_PER_HOOK,
        )
        descriptors: list[JsonDict | None] = []
        for row in pending_candidates:
            if permanently_out_of_scope(row):
                pending_rows.pop(canonical_uuid(str(row.get("id") or "")) or "", None)
                continue
            if eligible_rollout_thread(row):
                descriptors.append(descriptor_for_row(row))
        pending_descriptors = [item for item in descriptors if item is not None]
        tracked_descriptors = tracked_descriptors_for_hook(files, hook_payload or {})
        if pending_descriptors and tracked_descriptors:
            tracked_quota = MAX_FILES_PER_HOOK // 2
            pending_quota = MAX_FILES_PER_HOOK - tracked_quota
        elif tracked_descriptors:
            tracked_quota = MAX_FILES_PER_HOOK
            pending_quota = 0
        else:
            tracked_quota = 0
            pending_quota = MAX_FILES_PER_HOOK
        tracked_cursor = int(state.get("tracked_cursor") or 0)
        tracked_count = len(tracked_descriptors)
        tracked_descriptors, _ = rotate_items(
            tracked_descriptors,
            tracked_cursor,
            tracked_quota,
        )
        descriptors = tracked_descriptors + pending_descriptors[:pending_quota]
        descriptors = list({str(item["session_id"]): item for item in descriptors}.values())
        attach_root_threads(descriptors, files)
        queued = 0
        captured_bytes = 0
        processed_files = 0
        per_file_budget = max(
            1,
            MAX_SYNC_BYTES_PER_HOOK // max(1, len(descriptors)),
        )
        state["databases"] = databases
        for descriptor in descriptors:
            if processed_files >= MAX_FILES_PER_HOOK or captured_bytes >= MAX_SYNC_BYTES_PER_HOOK:
                break
            file_queued, file_bytes, complete = sync_rollout_file(
                base,
                state,
                descriptor,
                max_bytes=min(
                    per_file_budget,
                    MAX_SYNC_BYTES_PER_HOOK - captured_bytes,
                ),
            )
            queued += file_queued
            captured_bytes += file_bytes
            processed_files += 1
            if complete:
                pending_rows.pop(str(descriptor["session_id"]), None)
        state["tracked_cursor"] = (
            (tracked_cursor + min(processed_files, len(tracked_descriptors))) % tracked_count
            if tracked_count
            else 0
        )
        write_state(base, state)
        # A later hook is also the recovery signal for chunks left pending by a
        # prior network failure, even when no new bytes were discovered.
        session_logging.try_auto_drain()
        return {
            "queued": queued,
            "eligible": len(descriptors),
            "errors": errors,
        }


@contextlib.contextmanager
def sync_lock(base: Path):
    path = base / "rollout-sync" / "sync.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def discover_rollout_threads(
    codex_home: str | Path | None,
    database_state: JsonDict,
) -> tuple[list[JsonDict], list[str], JsonDict]:
    merged: dict[str, JsonDict] = {}
    errors: list[str] = []
    next_database_state = dict(database_state)
    for database in publish_presence.native_database_candidates(codex_home):
        try:
            database_stat = database.stat()
            database_key = str(database)
            previous = (
                database_state.get(database_key)
                if isinstance(database_state.get(database_key), dict)
                else {}
            )
            same_database = (
                int(previous.get("device") or -1) == database_stat.st_dev
                and int(previous.get("inode") or -1) == database_stat.st_ino
            )
            watermark = (
                int(previous.get("watermark_ms") or 0)
                if same_database
                else initial_discovery_cutoff_ms()
            )
            seen_at_watermark = {
                str(value)
                for value in previous.get("ids_at_watermark", [])
                if isinstance(value, str)
            } if same_database else set()
            offset = 0
            observed: list[JsonDict] = []
            while True:
                page = publish_presence.read_recent_threads(
                    database,
                    cutoff_ms=watermark,
                    limit=DISCOVERY_PAGE_SIZE,
                    offset=offset,
                )
                if not page:
                    break
                observed.extend(page)
                for row in page:
                    row_updated = int(row.get("updated_at_ms") or 0)
                    thread_id = str(row.get("id") or "")
                    if row_updated == watermark and thread_id in seen_at_watermark:
                        continue
                    previous = merged.get(thread_id)
                    if previous is None or publish_presence.native_row_precedence(
                        row
                    ) > publish_presence.native_row_precedence(previous):
                        merged[thread_id] = row
                offset += len(page)
                if len(page) < DISCOVERY_PAGE_SIZE:
                    break
            if observed:
                next_watermark = max(
                    watermark,
                    max(int(row.get("updated_at_ms") or 0) for row in observed),
                )
                ids_at_watermark = sorted(
                    str(row.get("id") or "")
                    for row in observed
                    if int(row.get("updated_at_ms") or 0) == next_watermark
                )
            else:
                next_watermark = watermark
                ids_at_watermark = sorted(seen_at_watermark)
            next_database_state[database_key] = {
                "device": database_stat.st_dev,
                "inode": database_stat.st_ino,
                "watermark_ms": next_watermark,
                "ids_at_watermark": ids_at_watermark,
            }
        except (OSError, sqlite3.Error, ValueError) as exc:
            errors.append(f"{database}: {exc}")
    rows = sorted(
        merged.values(),
        key=lambda row: (int(row.get("updated_at_ms") or 0), str(row.get("id") or "")),
        reverse=True,
    )
    return rows, errors, next_database_state


def initial_discovery_cutoff_ms() -> int:
    raw = os.environ.get(INITIAL_LOOKBACK_SECONDS_ENV)
    try:
        seconds = max(0, int(raw)) if raw is not None else DEFAULT_INITIAL_LOOKBACK_SECONDS
    except ValueError:
        seconds = DEFAULT_INITIAL_LOOKBACK_SECONDS
    return max(0, int(time.time() * 1000) - seconds * 1000)


def rotate_items(items: list[Any], cursor: int, limit: int) -> tuple[list[Any], int]:
    if not items or limit <= 0:
        return [], 0 if not items else cursor % len(items)
    start = cursor % len(items)
    count = min(limit, len(items))
    selected = [items[(start + index) % len(items)] for index in range(count)]
    return selected, (start + count) % len(items)


def eligible_rollout_thread(row: JsonDict) -> bool:
    if not all(row.get(key) for key in ("id", "rollout_path", "cwd")):
        return False
    try:
        UUID(str(row["id"]))
    except ValueError:
        return False
    path = Path(str(row["rollout_path"])).expanduser()
    if not path.is_file():
        return False
    remote = row.get("git_origin_url") or session_logging.git_origin_remote(str(row["cwd"]))
    if remote:
        row["git_origin_url"] = str(remote)
    return session_logging.remote_belongs_to_org(
        str(remote) if remote else None,
        session_logging.ALLOWED_GITHUB_ORG,
    )


def permanently_out_of_scope(row: JsonDict) -> bool:
    remote = row.get("git_origin_url")
    return bool(remote) and not session_logging.remote_belongs_to_org(
        str(remote),
        session_logging.ALLOWED_GITHUB_ORG,
    )


def descriptor_for_row(row: JsonDict) -> JsonDict | None:
    path = Path(str(row["rollout_path"])).expanduser().resolve()
    session_meta = read_session_meta(path)
    payload = session_meta.get("payload") if isinstance(session_meta.get("payload"), dict) else {}
    session_id = canonical_uuid(str(row["id"]))
    if not session_id:
        return None
    parent_id = canonical_uuid(payload.get("parent_thread_id")) or canonical_uuid(
        publish_presence.parent_thread_id(row)
    )
    metadata: JsonDict = {
        "cwd": str(row["cwd"]),
        "transcript_path": str(path),
        "repo_remote": str(row["git_origin_url"]),
        "source": "rollout_sync",
    }
    for key in ("git_branch", "thread_source"):
        if row.get(key):
            value = str(row[key])
            if key != "thread_source" or SAFE_CATEGORY_PATTERN.fullmatch(value):
                metadata[key] = value
    category = safe_category(rollout_source_category(payload.get("source"), row.get("source")))
    if category:
        metadata["rollout_source_category"] = category
    if parent_id:
        metadata["parent_thread_id"] = parent_id
    return {
        "session_id": session_id,
        "path": path,
        "parent_thread_id": parent_id,
        "created_at": publish_presence.milliseconds_to_iso(
            row.get("created_at_ms") or row.get("updated_at_ms")
        ),
        "metadata": metadata,
    }


def read_session_meta(path: Path) -> JsonDict:
    try:
        with path.open("rb") as handle:
            first_line = handle.readline(MAX_CHUNK_BYTES + 1)
    except OSError:
        return {}
    if len(first_line) > MAX_CHUNK_BYTES:
        return {}
    try:
        record = json.loads(first_line.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return {}
    return record


def canonical_uuid(value: object) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None


def rollout_source_category(source: object, fallback: object) -> str | None:
    if isinstance(source, dict):
        subagent = source.get("subagent")
        if isinstance(subagent, dict):
            if isinstance(subagent.get("thread_spawn"), dict):
                return "subagent.thread_spawn"
            other = subagent.get("other")
            if isinstance(other, str) and other:
                return f"subagent.{other}"
            return "subagent"
        for key in source:
            if isinstance(key, str) and key:
                return key
    if isinstance(source, str) and source:
        return source
    if isinstance(fallback, str) and fallback:
        try:
            parsed = json.loads(fallback)
        except json.JSONDecodeError:
            return fallback
        return rollout_source_category(parsed, None)
    return None


def safe_category(value: str | None) -> str | None:
    return value if value and SAFE_CATEGORY_PATTERN.fullmatch(value) else None


def tracked_descriptors_for_hook(files: JsonDict, payload: JsonDict) -> list[JsonDict]:
    transcript_path = session_logging.first_string(payload, "transcript_path", "transcriptPath")
    session_id = canonical_uuid(session_logging.first_string(payload, "session_id", "sessionId"))
    result: list[JsonDict] = []
    for key, value in files.items():
        if not isinstance(value, dict):
            continue
        related = session_id and session_id in {
            canonical_uuid(str(key)),
            canonical_uuid(value.get("parent_thread_id")),
            canonical_uuid(value.get("root_thread_id")),
        }
        same_path = transcript_path and str(value.get("path")) == str(
            Path(transcript_path).expanduser().resolve()
        )
        if not related and not same_path:
            continue
        descriptor = descriptor_from_state(str(key), value)
        if descriptor:
            result.append(descriptor)
    return result


def descriptor_from_state(session_id: str, entry: JsonDict) -> JsonDict | None:
    path = Path(str(entry.get("path") or "")).expanduser()
    metadata = entry.get("metadata")
    canonical_session = canonical_uuid(session_id)
    if not canonical_session or not path.is_file() or not isinstance(metadata, dict):
        return None
    return {
        "session_id": canonical_session,
        "path": path.resolve(),
        "parent_thread_id": canonical_uuid(entry.get("parent_thread_id")),
        "created_at": str(entry.get("created_at") or session_logging.now_iso()),
        "metadata": dict(metadata),
    }


def attach_root_threads(descriptors: list[JsonDict], files: JsonDict) -> None:
    parents = {
        str(key): canonical_uuid(value.get("parent_thread_id"))
        for key, value in files.items()
        if isinstance(value, dict)
    }
    parents.update(
        {
            str(item["session_id"]): item.get("parent_thread_id")
            for item in descriptors
        }
    )
    for item in descriptors:
        current = item.get("parent_thread_id")
        seen = {str(item["session_id"])}
        root = current
        while isinstance(current, str) and current not in seen:
            seen.add(current)
            next_parent = parents.get(current)
            if not isinstance(next_parent, str):
                break
            root = next_parent
            current = next_parent
        if isinstance(root, str):
            item["metadata"]["root_thread_id"] = root


def sync_rollout_file(
    base: Path,
    state: JsonDict,
    descriptor: JsonDict,
    *,
    max_bytes: int,
) -> tuple[int, int, bool]:
    files = state.setdefault("files", {})
    path = Path(descriptor["path"])
    key = str(descriptor["session_id"])
    previous = files.get(key) if isinstance(files.get(key), dict) else {}
    queued = 0
    with path.open("rb") as handle:
        stat = os.fstat(handle.fileno())
        reset = file_was_replaced(handle, path, stat, previous)
        generation_index = int(previous.get("generation_index") or 0) + (1 if reset else 0)
        if not previous:
            generation_index = 0
            reset = True
        if reset:
            prefix_size = min(stat.st_size, FINGERPRINT_BYTES)
            entry: JsonDict = {
                "path": str(path),
                "device": stat.st_dev,
                "inode": stat.st_ino,
                "prefix_size": prefix_size,
                "prefix_sha256": hash_prefix(handle, prefix_size),
                "generation_index": generation_index,
                "generation": generation_token(path, stat, generation_index),
                "offset": 0,
            }
            files[key] = entry
        else:
            entry = previous
        entry.update(
            {
                "created_at": descriptor["created_at"],
                "parent_thread_id": descriptor.get("parent_thread_id"),
                "root_thread_id": descriptor["metadata"].get("root_thread_id"),
                "metadata": descriptor["metadata"],
            }
        )
        stable_end = stat.st_size
        offset = int(entry.get("offset") or 0)
        initial_offset = offset
        if stable_end <= offset:
            return 0, 0, True
        capture_end = min(stable_end, offset + max(0, max_bytes))
        if capture_end <= offset:
            return 0, 0, False
        handle.seek(offset)
        while offset < capture_end:
            content = handle.read(min(MAX_CHUNK_BYTES, capture_end - offset))
            if not content:
                break
            end_offset = offset + len(content)
            record = rollout_chunk_record(
                base,
                descriptor=descriptor,
                generation=str(entry["generation"]),
                start_offset=offset,
                end_offset=end_offset,
                content=content,
            )
            session_logging.enqueue_record(base, record)
            entry["offset"] = end_offset
            write_state(base, state)
            offset = end_offset
            queued += 1
    return queued, max(0, offset - initial_offset), offset >= stable_end


def file_was_replaced(
    handle: Any,
    path: Path,
    stat: os.stat_result,
    previous: JsonDict,
) -> bool:
    if not previous:
        return True
    if str(previous.get("path")) != str(path):
        return True
    if int(previous.get("device") or -1) != stat.st_dev or int(previous.get("inode") or -1) != stat.st_ino:
        return True
    if stat.st_size < int(previous.get("offset") or 0):
        return True
    prefix_size = int(previous.get("prefix_size") or 0)
    return prefix_size > stat.st_size or hash_prefix(handle, prefix_size) != previous.get("prefix_sha256")


def generation_token(path: Path, stat: os.stat_result, generation_index: int) -> str:
    value = f"rollout-generation:v1:{path}:{stat.st_dev}:{stat.st_ino}:{generation_index}"
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def hash_prefix(handle: Any, size: int) -> str:
    position = handle.tell()
    try:
        handle.seek(0)
        return hashlib.sha256(handle.read(max(0, size))).hexdigest()
    finally:
        handle.seek(position)


def rollout_chunk_record(
    base: Path,
    *,
    descriptor: JsonDict,
    generation: str,
    start_offset: int,
    end_offset: int,
    content: bytes,
) -> JsonDict:
    content_sha256 = hashlib.sha256(content).hexdigest()
    identity = (
        f"rollout-chunk:v1:{descriptor['session_id']}:{generation}:"
        f"{start_offset}:{end_offset}:{content_sha256}"
    )
    event_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:32]
    local_path = f"rollout-sync/chunks/{event_id}.json"
    detail: JsonDict = {
        "file_generation": generation,
        "start_offset": start_offset,
        "end_offset": end_offset,
        "content_sha256": content_sha256,
        "content_byte_size": len(content),
        "content_base64": base64.b64encode(content).decode("ascii"),
    }
    session_logging.write_json_atomic(base / local_path, detail)
    metadata = {
        **descriptor["metadata"],
        "file_generation": generation,
        "start_offset": start_offset,
        "end_offset": end_offset,
        "content_sha256": content_sha256,
        "content_byte_size": len(content),
    }
    return {
        "id": event_id,
        "type": "event",
        "session_id": descriptor["session_id"],
        "thread_id": session_logging.sha256_hex(str(descriptor["path"])),
        "seq": int(event_id[:7], 16),
        "event_type": "rollout_chunk",
        "hook_event_name": "RolloutSync",
        "storage_bucket": session_logging.bucket_name(),
        "storage_path": None,
        "local_content_path": local_path,
        "metadata": metadata,
        "created_at": descriptor["created_at"],
        "uploaded_at": None,
    }


def read_state(base: Path) -> JsonDict:
    path = base / "rollout-sync" / "state.json"
    try:
        state = session_logging.read_json_file(path)
    except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
        return {"version": STATE_VERSION, "files": {}}
    if state.get("version") != STATE_VERSION or not isinstance(state.get("files"), dict):
        return {"version": STATE_VERSION, "files": {}}
    return state


def write_state(base: Path, state: JsonDict) -> None:
    state["version"] = STATE_VERSION
    session_logging.write_json_atomic(base / "rollout-sync" / "state.json", state)
