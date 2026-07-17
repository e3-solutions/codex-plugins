#!/usr/bin/env python3
"""Idempotent backfill of recent Claude Code sessions into the shared ingest.

Mirrors ``codex-session-logging``'s ``backfill_sessions.py``: it replays local
transcripts (``~/.claude/projects/*/*.jsonl``) scoped to e3-solutions repos and
re-emits metadata-only session-lifecycle events — a ``thread_started`` at the
first activity time and a ``thread_ended`` at the last — tagged ``agent=claude``
and ``source=historical_transcript`` through the plugin's existing queue. It
reads only timestamps, cwd, and repo/branch; never prompts, responses, tool
calls, or transcript bodies. Runs are idempotent: files are fingerprinted and
event ids are deterministic, so re-running never duplicates rows.

NOTE: the shared ingest currently drops ``source=historical_transcript`` records
without writes (identical to Codex — see ``isHistoricalBackfill`` in the ingest).
This script is therefore parity-complete and safe to run today, but its records
only land once historical ingestion is enabled server-side.
"""
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import publish_presence
import session_logging

JsonDict = dict[str, Any]

BACKFILL_VERSION = 1
BACKFILL_ENABLED_ENV = "CLAUDE_SESSION_LOG_BACKFILL"
BACKFILL_HOURS_ENV = "CLAUDE_SESSION_LOG_BACKFILL_HOURS"
BACKFILL_MAX_FILES_ENV = "CLAUDE_SESSION_LOG_BACKFILL_MAX_FILES"
DEFAULT_HOURS = 48
DEFAULT_MAX_FILES = 1000


def backfill_enabled() -> bool:
    value = os.environ.get(BACKFILL_ENABLED_ENV, "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def _int_env(name: str, default: int) -> int:
    try:
        return max(1, int(os.environ.get(name, default)))
    except (TypeError, ValueError):
        return default


def backfill_dir(base: Path) -> Path:
    return base / "backfills" / f"v{BACKFILL_VERSION}"


def state_path(base: Path) -> Path:
    return backfill_dir(base) / "state.json"


def read_state(base: Path) -> JsonDict:
    try:
        loaded = json.loads(state_path(base).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"version": BACKFILL_VERSION, "files": {}}
    if not isinstance(loaded, dict):
        return {"version": BACKFILL_VERSION, "files": {}}
    loaded.setdefault("files", {})
    return loaded


def file_fingerprint(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def deterministic_uuid(value: str) -> str:
    digest = bytearray(hashlib.sha256(value.encode("utf-8")).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x50
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


def iter_timestamps(path: Path) -> Iterator[str]:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                loaded = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(loaded, dict) and isinstance(loaded.get("timestamp"), str):
                yield loaded["timestamp"]


def session_bounds(path: Path) -> tuple[str | None, str | None]:
    first: str | None = None
    last: str | None = None
    for timestamp in iter_timestamps(path):
        if first is None:
            first = timestamp
        last = timestamp
    return first, last


def _lifecycle_record(
    target: JsonDict,
    *,
    event_type: str,
    hook_event_name: str,
    thread_event: str,
    created_at: str,
    base: Path,
    ended: bool = False,
) -> JsonDict:
    session_id = session_logging.safe_segment(str(target["session_id"]))
    transcript_path = str(target["transcript_path"])
    event_id = deterministic_uuid(f"claude-backfill-v{BACKFILL_VERSION}:{session_id}:{event_type}")
    seq = 0 if event_type == "thread_started" else 1
    storage_path = (
        f"users/local/sessions/{session_id}/events/{seq:06d}-{event_type}.json"
    )
    metadata: JsonDict = {
        "platform": session_logging.PLATFORM,
        "agent": session_logging.AGENT,
        "cwd": str(target["cwd"]),
        "transcript_path": transcript_path,
        "source": "historical_transcript",
        "thread_event": thread_event,
    }
    branch = target.get("git_branch")
    if isinstance(branch, str) and branch:
        metadata["git_branch"] = branch
    detail: JsonDict = {
        "id": event_id,
        "session_id": session_id,
        "seq": seq,
        "event_type": event_type,
        "hook_event_name": hook_event_name,
        "created_at": created_at,
        "metadata": metadata,
        "thread_id": session_logging.sha256_hex(transcript_path),
    }
    session_logging.write_json_atomic(base / storage_path, detail)
    record: JsonDict = {
        **detail,
        "type": "event",
        "storage_bucket": session_logging.bucket_name(),
        "storage_path": storage_path,
        "local_content_path": storage_path,
        "uploaded_at": None,
    }
    if ended:
        record["ended_at"] = created_at
    return record


def import_transcript(path: Path, *, base: Path, dry_run: bool) -> JsonDict:
    target = publish_presence.session_target(path)
    if target is None:
        return {"status": "skipped_non_e3", "queued": 0}
    first, last = session_bounds(path)
    fallback = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()
    started_at = first or fallback
    ended_at = last or started_at
    queued = 0
    if not dry_run:
        started = _lifecycle_record(
            target,
            event_type="thread_started",
            hook_event_name="HistoricalBackfill",
            thread_event="started",
            created_at=started_at,
            base=base,
        )
        session_logging.enqueue_record(base, started)
        ended = _lifecycle_record(
            target,
            event_type="thread_ended",
            hook_event_name="HistoricalBackfill",
            thread_event="ended",
            created_at=ended_at,
            base=base,
            ended=True,
        )
        session_logging.enqueue_record(base, ended)
    queued += 2
    return {"status": "complete", "queued": queued, "session_id": target["session_id"]}


def run_backfill(*, dry_run: bool = False, force: bool = False, hours: int | None = None,
                 max_files: int | None = None) -> JsonDict:
    if not backfill_enabled():
        return {"version": BACKFILL_VERSION, "status": "disabled"}
    hours = hours if hours is not None else _int_env(BACKFILL_HOURS_ENV, DEFAULT_HOURS)
    max_files = max_files if max_files is not None else _int_env(BACKFILL_MAX_FILES_ENV, DEFAULT_MAX_FILES)
    base = session_logging.ensure_state_dir()
    directory = backfill_dir(base)
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / "worker.lock"
    with lock_path.open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"version": BACKFILL_VERSION, "status": "already_running"}

        state = read_state(base)
        files = state.setdefault("files", {})
        cutoff = datetime.now(timezone.utc).timestamp() - hours * 3600
        totals = {"discovered": 0, "processed": 0, "queued": 0, "skipped_non_e3": 0, "failed": 0}

        for path in publish_presence.iter_session_files(publish_presence.default_projects_dir()):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff:
                continue
            totals["discovered"] += 1
            if totals["processed"] >= max_files:
                break
            key = str(path)
            try:
                fingerprint = file_fingerprint(path)
            except OSError:
                totals["failed"] += 1
                continue
            previous = files.get(key) if isinstance(files.get(key), dict) else {}
            if not force and previous.get("fingerprint") == fingerprint and \
                    previous.get("status") in {"complete", "skipped_non_e3"}:
                continue
            try:
                result = import_transcript(path, base=base, dry_run=dry_run)
            except Exception as exc:  # noqa: BLE001 - one bad transcript must not stop the run.
                totals["failed"] += 1
                files[key] = {"fingerprint": fingerprint, "status": "failed", "error": str(exc)}
                continue
            totals["processed"] += 1
            totals["queued"] += int(result.get("queued", 0))
            if result.get("status") == "skipped_non_e3":
                totals["skipped_non_e3"] += 1
            files[key] = {"fingerprint": fingerprint, "status": result["status"],
                          "updated_at": session_logging.now_iso()}

        if not dry_run:
            session_logging.write_json_atomic(state_path(base), state)
            drain = session_logging.drain_queue() if totals["queued"] else {"uploaded": 0}
        else:
            drain = {"uploaded": 0, "dry_run": True}
        return {"version": BACKFILL_VERSION, "status": "complete", **totals, "drain": drain}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Backfill recent Claude Code sessions.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--hours", type=int, default=None)
    parser.add_argument("--max-files", type=int, default=None)
    args = parser.parse_args(argv)
    print(json.dumps(
        run_backfill(dry_run=args.dry_run, force=args.force, hours=args.hours, max_files=args.max_files),
        sort_keys=True,
    ))


if __name__ == "__main__":
    main()
