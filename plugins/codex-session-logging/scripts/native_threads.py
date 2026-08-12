from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import UUID


JsonDict = dict[str, Any]
DEFAULT_LIMIT = 500


def default_codex_home() -> Path:
    override = os.environ.get("CODEX_HOME")
    return Path(override).expanduser().resolve() if override else Path.home() / ".codex"


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
            "source": "source" if "source" in columns else "null",
            "thread_source": "thread_source" if "thread_source" in columns else "null",
            "archived": "archived" if "archived" in columns else "0",
        }
        query = f"""
            select
                id,
                rollout_path,
                cwd,
                {optional['git_origin_url']} as git_origin_url,
                {optional['git_branch']} as git_branch,
                {optional['source']} as source,
                {optional['thread_source']} as thread_source,
                {optional['archived']} as archived,
                {created} as created_at_ms,
                {updated} as updated_at_ms
            from threads
            where {updated} >= ?
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


def milliseconds_to_iso(value: object) -> str:
    milliseconds = int(value)
    return datetime.fromtimestamp(milliseconds / 1000, tz=timezone.utc).isoformat()


def native_row_precedence(row: JsonDict) -> tuple[int, int]:
    source = row.get("thread_source")
    suppresses_capture = bool(row.get("archived")) or source not in (None, "", "user", "subagent")
    return int(row["updated_at_ms"]), int(suppresses_capture)


def parent_thread_id(row: JsonDict) -> str | None:
    if row.get("thread_source") != "subagent":
        return None
    source = row.get("source")
    if not isinstance(source, str) or not source:
        return None
    try:
        payload = json.loads(source)
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    subagent = payload.get("subagent")
    if not isinstance(subagent, dict):
        return None
    thread_spawn = subagent.get("thread_spawn")
    if not isinstance(thread_spawn, dict):
        return None
    value = thread_spawn.get("parent_thread_id")
    if not isinstance(value, str) or not value:
        return None
    try:
        return str(UUID(value))
    except ValueError:
        return None
