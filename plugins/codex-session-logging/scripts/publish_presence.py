#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import session_logging


JsonDict = dict[str, Any]
PRESENCE_STATE_ENV = "CODEX_SESSION_PRESENCE_STATE"
DEFAULT_LOOKBACK_SECONDS = 24 * 60 * 60
DEFAULT_LIMIT = 500
PRESENCE_SCHEMA_VERSION = 1
MAX_PRESENCE_UPLOAD_AGE_SECONDS = 5 * 60


def default_codex_home() -> Path:
    override = os.environ.get("CODEX_HOME")
    return Path(override).expanduser().resolve() if override else Path.home() / ".codex"


def default_state_path(codex_home: str | Path | None = None) -> Path:
    override = os.environ.get(PRESENCE_STATE_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return Path(codex_home or default_codex_home()).expanduser().resolve() / "coreedge" / "presence" / "state.json"


def native_database_candidates(codex_home: str | Path | None = None) -> list[Path]:
    home = Path(codex_home or default_codex_home()).expanduser().resolve()
    candidates: dict[Path, int] = {}
    for root in (home, home / "sqlite"):
        for pattern in ("state_*.sqlite", "state.sqlite"):
            for path in root.glob(pattern):
                try:
                    resolved = path.resolve()
                    if resolved.is_file():
                        activity_mtime = resolved.stat().st_mtime_ns
                        wal_path = Path(f"{resolved}-wal")
                        if wal_path.is_file():
                            activity_mtime = max(activity_mtime, wal_path.stat().st_mtime_ns)
                        candidates[resolved] = activity_mtime
                except OSError:
                    continue
    return sorted(candidates, key=lambda path: (candidates[path], str(path)), reverse=True)


def table_columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {str(row[1]) for row in connection.execute(f"pragma table_info({table})")}


def timestamp_expression(columns: set[str], *, seconds: str, milliseconds: str) -> str | None:
    if milliseconds in columns and seconds in columns:
        return f"coalesce({milliseconds}, {seconds} * 1000)"
    if milliseconds in columns:
        return milliseconds
    if seconds in columns:
        return f"{seconds} * 1000"
    return None


def read_recent_threads(
    database: str | Path,
    *,
    cutoff_ms: int,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> list[JsonDict]:
    path = Path(database).expanduser().resolve()
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True, timeout=2)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("pragma query_only = on")
        connection.execute("pragma busy_timeout = 2000")
        columns = table_columns(connection, "threads")
        required = {"id", "rollout_path", "cwd"}
        if not required.issubset(columns):
            missing = ", ".join(sorted(required - columns))
            raise ValueError(f"native Codex threads schema is missing: {missing}")
        updated = timestamp_expression(columns, seconds="updated_at", milliseconds="updated_at_ms")
        created = timestamp_expression(columns, seconds="created_at", milliseconds="created_at_ms")
        if updated is None:
            raise ValueError("native Codex threads schema has no supported updated timestamp")
        created = created or updated
        optional = {
            "git_branch": "git_branch" if "git_branch" in columns else "null",
            "git_origin_url": "git_origin_url" if "git_origin_url" in columns else "null",
            "thread_source": "thread_source" if "thread_source" in columns else "null",
            "archived": "archived" if "archived" in columns else "0",
        }
        predicates = [f"{updated} >= ?"]
        query = f"""
            select
                id,
                rollout_path,
                cwd,
                {optional['git_origin_url']} as git_origin_url,
                {optional['git_branch']} as git_branch,
                {optional['thread_source']} as thread_source,
                {optional['archived']} as archived,
                {created} as created_at_ms,
                {updated} as updated_at_ms
            from threads
            where {' and '.join(predicates)}
            order by {updated} desc, id desc
            limit ? offset ?
        """
        return [
            dict(row)
            for row in connection.execute(
                query,
                (cutoff_ms, max(1, int(limit)), max(0, int(offset))),
            )
        ]
    finally:
        connection.close()


def discover_recent_threads(
    codex_home: str | Path | None = None,
    *,
    cutoff_ms: int,
    limit: int = DEFAULT_LIMIT,
) -> tuple[Path | None, list[JsonDict], list[str]]:
    errors: list[str] = []
    primary: Path | None = None
    merged: dict[str, JsonDict] = {}
    for candidate in native_database_candidates(codex_home):
        try:
            offset = 0
            while True:
                page = read_recent_threads(
                    candidate,
                    cutoff_ms=cutoff_ms,
                    limit=DEFAULT_LIMIT,
                    offset=offset,
                )
                if not page:
                    break
                if primary is None:
                    primary = candidate
                for row in page:
                    key = str(row.get("id") or "")
                    previous = merged.get(key)
                    if previous is None or native_row_precedence(row) > native_row_precedence(previous):
                        merged[key] = row
                offset += len(page)
                if len(page) < DEFAULT_LIMIT:
                    break
            if primary is None:
                primary = candidate
        except (OSError, sqlite3.Error, ValueError) as exc:
            errors.append(f"{candidate}: {exc}")
    rows = sorted(
        merged.values(),
        key=lambda row: (int(row["updated_at_ms"]), str(row.get("id") or "")),
        reverse=True,
    )
    eligible: list[JsonDict] = []
    for row in rows:
        if eligible_thread(row):
            eligible.append(row)
            if len(eligible) >= max(1, int(limit)):
                break
    return primary, eligible, errors


def milliseconds_to_iso(value: object) -> str:
    milliseconds = int(value)
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc).isoformat()


def native_row_precedence(row: JsonDict) -> tuple[int, int]:
    source = row.get("thread_source")
    suppresses_presence = bool(row.get("archived")) or source not in (None, "", "user")
    return int(row["updated_at_ms"]), int(suppresses_presence)


def eligible_thread(row: JsonDict) -> bool:
    if bool(row.get("archived")):
        return False
    if row.get("thread_source") not in (None, "", "user"):
        return False
    required = ("id", "rollout_path", "cwd", "updated_at_ms")
    if any(not row.get(key) for key in required):
        return False
    remote = row.get("git_origin_url") or session_logging.git_origin_remote(str(row["cwd"]))
    if remote:
        row["git_origin_url"] = str(remote)
    return session_logging.remote_belongs_to_org(
        str(remote) if remote else None,
        session_logging.ALLOWED_GITHUB_ORG,
    )


def presence_record(row: JsonDict, *, base: Path) -> JsonDict:
    session_id = session_logging.safe_segment(str(row["id"]))
    transcript_path = str(row["rollout_path"])
    observed_at = milliseconds_to_iso(row["updated_at_ms"])
    created_at = milliseconds_to_iso(row.get("created_at_ms") or row["updated_at_ms"])
    event_id = session_logging.sha256_hex(f"resident-presence:{session_id}")[:32]
    event_type = "resident_presence"
    storage_path = f"users/local/sessions/{session_id}/events/000000-{event_type}.json"
    metadata: JsonDict = {
        "cwd": str(row["cwd"]),
        "transcript_path": transcript_path,
        "repo_remote": str(row["git_origin_url"]),
        "source": "resident_presence",
        "native_created_at": created_at,
        "native_updated_at": observed_at,
    }
    for key in ("git_branch", "thread_source"):
        if row.get(key):
            metadata[key] = str(row[key])
    detail: JsonDict = {
        "id": event_id,
        "session_id": session_id,
        "seq": 0,
        "event_type": event_type,
        "hook_event_name": "ResidentPresence",
        "created_at": observed_at,
        "metadata": metadata,
        "thread_id": session_logging.sha256_hex(transcript_path),
    }
    session_logging.write_json_atomic(base / storage_path, detail)
    return {
        **detail,
        "type": "event",
        "storage_bucket": session_logging.bucket_name(),
        "storage_path": storage_path,
        "local_content_path": storage_path,
        "uploaded_at": None,
    }


def presence_queue_dir(base: Path) -> Path:
    return base / "presence-queue" / "pending"


def presence_dead_letter_dir(base: Path) -> Path:
    return base / "presence-queue" / "dead-letter"


def presence_queue_path(base: Path, record: JsonDict) -> Path:
    return presence_queue_dir(base) / f"{session_logging.safe_segment(str(record['id']))}.json"


def enqueue_presence(base: Path, record: JsonDict) -> None:
    session_logging.write_json_atomic(presence_queue_path(base, record), record)


def upload_presence_queue(
    base: Path,
    *,
    now: datetime | None = None,
) -> tuple[JsonDict, list[JsonDict], list[JsonDict]]:
    pending = presence_queue_dir(base)
    pending.mkdir(parents=True, exist_ok=True)
    uploader = session_logging.IngestUploader.from_env()
    uploaded_records: list[JsonDict] = []
    expired_records: list[JsonDict] = []
    failed = 0
    dead_lettered = 0
    for path in sorted(pending.glob("*.json")):
        try:
            record = session_logging.read_json_file(path)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            failed_at = session_logging.now_iso()
            target = presence_dead_letter_dir(base) / path.name
            session_logging.write_json_atomic(
                target,
                {
                    "id": path.stem,
                    "last_upload_error": str(exc),
                    "last_upload_failed_at": failed_at,
                    "dead_letter_reason": "invalid_local_record",
                    "dead_lettered_at": failed_at,
                },
            )
            path.unlink(missing_ok=True)
            dead_lettered += 1
            continue
        try:
            observed_at = datetime.fromisoformat(str(record["created_at"]))
            if observed_at.tzinfo is None:
                observed_at = observed_at.replace(tzinfo=timezone.utc)
            age = ((now or datetime.now(timezone.utc)) - observed_at.astimezone(timezone.utc)).total_seconds()
        except (KeyError, TypeError, ValueError):
            age = 0
        if age > MAX_PRESENCE_UPLOAD_AGE_SECONDS:
            expired_records.append(record)
            (presence_dead_letter_dir(base) / path.name).unlink(missing_ok=True)
            path.unlink(missing_ok=True)
            continue
        try:
            uploader.upload_message(record, base=base)
        except session_logging.PermanentUploadError as exc:
            failed_at = session_logging.now_iso()
            record.update(
                {
                    "last_upload_error": str(exc),
                    "last_upload_failed_at": failed_at,
                    "dead_letter_reason": "permanent_upload_failure",
                    "dead_lettered_at": failed_at,
                }
            )
            target = presence_dead_letter_dir(base) / path.name
            session_logging.write_json_atomic(target, record)
            path.unlink(missing_ok=True)
            dead_lettered += 1
        except Exception as exc:  # noqa: BLE001 - presence must remain durable while offline.
            failed += 1
            record["last_upload_error"] = str(exc)
            record["last_upload_failed_at"] = session_logging.now_iso()
            session_logging.write_json_atomic(path, record)
            break
        else:
            uploaded_records.append(record)
            (presence_dead_letter_dir(base) / path.name).unlink(missing_ok=True)
            path.unlink(missing_ok=True)
    return (
        {
            "uploaded": len(uploaded_records),
            "failed": failed,
            "dead_lettered": dead_lettered,
            "expired": len(expired_records),
            "remaining": len(list(pending.glob("*.json"))),
        },
        uploaded_records,
        expired_records,
    )


def load_state(path: Path) -> JsonDict:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        state = {}
    if not isinstance(state, dict):
        state = {}
    published = state.get("published")
    if not isinstance(published, dict):
        state["published"] = {}
    return state


def write_state(path: Path, state: JsonDict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def run_presence(
    *,
    codex_home: str | Path | None = None,
    state_path: str | Path | None = None,
    now: datetime | None = None,
    lookback_seconds: int = DEFAULT_LOOKBACK_SECONDS,
    limit: int = DEFAULT_LIMIT,
) -> JsonDict:
    home = Path(codex_home or default_codex_home()).expanduser().resolve()
    path = Path(state_path or default_state_path(home)).expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = path.with_suffix(".lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"locked": True, "published": 0, "queued": 0}

        current = now or datetime.now(timezone.utc)
        checked_at = current.isoformat()
        cutoff_ms = int(current.timestamp() * 1000) - max(1, int(lookback_seconds)) * 1000
        state = load_state(path)
        state.update({"schema": PRESENCE_SCHEMA_VERSION, "last_checked_at": checked_at})
        published_state = state["published"]
        assert isinstance(published_state, dict)
        state["published"] = {
            key: value
            for key, value in published_state.items()
            if isinstance(value, dict) and int(value.get("updated_at_ms") or 0) >= cutoff_ms
        }
        published_state = state["published"]

        if not session_logging.upload_configured():
            state.update({"last_result": "disabled", "last_error": None})
            write_state(path, state)
            return {"disabled": True, "published": 0, "queued": 0}

        database, rows, errors = discover_recent_threads(home, cutoff_ms=cutoff_ms, limit=limit)
        if database is None:
            message = "; ".join(errors) if errors else "native Codex state database was not found"
            state.update({"last_result": "unavailable", "last_error": message})
            write_state(path, state)
            return {"database": None, "errors": errors, "published": 0, "queued": 0}

        base = session_logging.ensure_state_dir()
        candidates: list[tuple[JsonDict, JsonDict]] = []
        for row in rows:
            if not eligible_thread(row):
                continue
            session_id = session_logging.safe_segment(str(row["id"]))
            updated_ms = int(row["updated_at_ms"])
            previous = published_state.get(session_id)
            if isinstance(previous, dict) and int(previous.get("updated_at_ms") or 0) >= updated_ms:
                continue
            record = presence_record(row, base=base)
            enqueue_presence(base, record)
            candidates.append((row, record))

        drain, uploaded_records, expired_records = upload_presence_queue(base, now=current)
        for acknowledged_record in [*uploaded_records, *expired_records]:
            session_id = session_logging.safe_segment(str(acknowledged_record["session_id"]))
            metadata = acknowledged_record.get("metadata")
            native_updated = metadata.get("native_updated_at") if isinstance(metadata, dict) else None
            if not isinstance(native_updated, str):
                continue
            updated_at_ms = int(datetime.fromisoformat(native_updated).timestamp() * 1000)
            previous = published_state.get(session_id)
            if isinstance(previous, dict) and int(previous.get("updated_at_ms") or 0) > updated_at_ms:
                continue
            published_state[session_id] = {
                "updated_at_ms": updated_at_ms,
                "published_at": checked_at,
            }

        published_ids = {str(record.get("id")) for record in uploaded_records}
        expired_ids = {str(record.get("id")) for record in expired_records}
        published = sum(1 for _row, record in candidates if str(record["id"]) in published_ids)
        expired = sum(1 for _row, record in candidates if str(record["id"]) in expired_ids)
        failed = len(candidates) - published - expired
        queue_issues = int(drain.get("failed") or 0) + int(drain.get("dead_lettered") or 0)
        if drain.get("uploaded"):
            last_result = "published"
        elif drain.get("remaining") or queue_issues:
            last_result = "retrying"
        else:
            last_result = "no_change"
        state.update(
            {
                "database": str(database),
                "last_result": last_result,
                "last_error": (
                    None
                    if queue_issues == 0
                    else f"{queue_issues} presence record(s) failed or were dead-lettered"
                ),
                "last_published": int(drain.get("uploaded") or 0),
                "last_expired": int(drain.get("expired") or 0),
                "last_queued": len(candidates),
            }
        )
        write_state(path, state)
        return {
            "database": str(database),
            "eligible": sum(1 for row in rows if eligible_thread(row)),
            "published": published,
            "queued": len(candidates),
            "failed": max(failed, queue_issues),
            "drain": drain,
        }


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Publish metadata-only presence for native Codex tasks.")
    parser.add_argument("--codex-home")
    parser.add_argument("--state-path")
    parser.add_argument("--lookback-seconds", type=int, default=DEFAULT_LOOKBACK_SECONDS)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_presence(
            codex_home=args.codex_home,
            state_path=args.state_path,
            lookback_seconds=args.lookback_seconds,
            limit=args.limit,
        )
    except Exception as exc:  # noqa: BLE001 - resident tracking must never disrupt Codex.
        result = {"error": str(exc), "published": 0, "queued": 0}
    if not args.quiet or result.get("error") or result.get("failed") or result.get("published"):
        print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
