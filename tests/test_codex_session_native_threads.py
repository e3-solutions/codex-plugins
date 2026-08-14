from __future__ import annotations

import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "plugins/codex-session-logging/scripts/native_threads.py"
PARENT_ID = "019f02bd-5d00-7e22-8e1a-4a30e7261c9f"


def load_native_threads():
    spec = importlib.util.spec_from_file_location("native_threads", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def create_database(path: Path, *, legacy: bool = False) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    if legacy:
        connection.execute(
            """
            create table threads (
                id text primary key,
                rollout_path text not null,
                created_at integer not null,
                updated_at integer not null,
                cwd text not null
            )
            """
        )
    else:
        connection.execute(
            """
            create table threads (
                id text primary key,
                rollout_path text not null,
                created_at integer not null,
                updated_at integer not null,
                source text,
                cwd text not null,
                archived integer not null default 0,
                git_branch text,
                git_origin_url text,
                created_at_ms integer,
                updated_at_ms integer,
                thread_source text
            )
            """
        )
    connection.commit()
    connection.close()
    return path


def test_native_database_candidates_use_database_and_wal_activity(tmp_path):
    module = load_native_threads()
    older = create_database(tmp_path / "state_1.sqlite")
    newer = create_database(tmp_path / "sqlite" / "state_2.sqlite")
    os.utime(older, ns=(10, 10))
    os.utime(newer, ns=(20, 20))

    wal = Path(f"{older}-wal")
    wal.write_bytes(b"active")
    os.utime(wal, ns=(30, 30))

    assert module.native_database_candidates(tmp_path) == [older.resolve(), newer.resolve()]


def test_read_recent_threads_is_bounded_and_omits_message_preview_columns(tmp_path):
    module = load_native_threads()
    database = create_database(tmp_path / "state.sqlite")
    connection = sqlite3.connect(database)
    connection.execute("alter table threads add column title text")
    connection.execute("alter table threads add column preview text")
    for index in range(3):
        connection.execute(
            """
            insert into threads (
                id, rollout_path, created_at, updated_at, source, cwd, archived,
                git_branch, git_origin_url, created_at_ms, updated_at_ms,
                thread_source, title, preview
            ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                f"thread-{index}",
                f"/tmp/{index}.jsonl",
                index + 1,
                index + 1,
                "vscode",
                f"/tmp/repo-{index}",
                0,
                "arya/test",
                "https://github.com/e3-solutions/example.git",
                (index + 1) * 1000,
                (index + 1) * 1000,
                "user",
                "sensitive title",
                "sensitive preview",
            ),
        )
    connection.commit()
    connection.close()

    rows = module.read_recent_threads(database, cutoff_ms=1000, limit=1, offset=1)

    assert [row["id"] for row in rows] == ["thread-1"]
    assert "title" not in rows[0]
    assert "preview" not in rows[0]


def test_read_recent_threads_supports_legacy_seconds_schema(tmp_path):
    module = load_native_threads()
    database = create_database(tmp_path / "state.sqlite", legacy=True)
    connection = sqlite3.connect(database)
    connection.execute(
        "insert into threads (id, rollout_path, created_at, updated_at, cwd) values (?, ?, ?, ?, ?)",
        ("legacy", "/tmp/legacy.jsonl", 10, 20, "/tmp/repo"),
    )
    connection.commit()
    connection.close()

    rows = module.read_recent_threads(database, cutoff_ms=19_000)

    assert rows == [{
        "id": "legacy",
        "rollout_path": "/tmp/legacy.jsonl",
        "cwd": "/tmp/repo",
        "git_origin_url": None,
        "git_branch": None,
        "source": None,
        "thread_source": None,
        "archived": 0,
        "created_at_ms": 10_000,
        "updated_at_ms": 20_000,
    }]


def test_read_recent_threads_rejects_incompatible_schema(tmp_path):
    module = load_native_threads()
    database = tmp_path / "state.sqlite"
    connection = sqlite3.connect(database)
    connection.execute("create table threads (id text primary key, title text)")
    connection.commit()
    connection.close()

    with pytest.raises(ValueError, match="cwd, rollout_path"):
        module.read_recent_threads(database, cutoff_ms=0)


def test_native_row_precedence_and_parent_linkage_are_deterministic():
    module = load_native_threads()
    live = {"updated_at_ms": 1000, "archived": 0, "thread_source": "subagent"}
    archived = {"updated_at_ms": 1000, "archived": 1, "thread_source": "subagent"}
    source = json.dumps({
        "subagent": {"thread_spawn": {"parent_thread_id": PARENT_ID}}
    })

    assert module.native_row_precedence(archived) > module.native_row_precedence(live)
    assert module.parent_thread_id({**live, "source": source}) == PARENT_ID
    assert module.parent_thread_id({**live, "source": "not-json"}) is None
    assert module.milliseconds_to_iso(1000) == "1970-01-01T00:00:01+00:00"
