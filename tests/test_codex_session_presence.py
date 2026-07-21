from __future__ import annotations

import fcntl
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-session-logging" / "scripts"


@pytest.fixture(autouse=True)
def isolated_codex_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "default-codex-home"))


def load_presence():
    logging_spec = importlib.util.spec_from_file_location("session_logging", SCRIPTS / "session_logging.py")
    session_logging = importlib.util.module_from_spec(logging_spec)
    assert logging_spec.loader is not None
    sys.modules[logging_spec.name] = session_logging
    logging_spec.loader.exec_module(session_logging)

    presence_spec = importlib.util.spec_from_file_location("publish_presence", SCRIPTS / "publish_presence.py")
    presence = importlib.util.module_from_spec(presence_spec)
    assert presence_spec.loader is not None
    sys.modules[presence_spec.name] = presence
    presence_spec.loader.exec_module(presence)
    return presence


def create_native_database(path: Path, rows: list[dict], *, valid: bool = True) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    if not valid:
        connection.execute("create table threads (id text primary key, title text)")
        connection.commit()
        connection.close()
        return path
    connection.execute(
        """
        create table threads (
            id text primary key,
            rollout_path text not null,
            created_at integer not null,
            updated_at integer not null,
            source text not null,
            cwd text not null,
            title text not null,
            preview text not null,
            first_user_message text not null,
            archived integer not null default 0,
            git_branch text,
            git_origin_url text,
            created_at_ms integer,
            updated_at_ms integer,
            thread_source text
        )
        """
    )
    for row in rows:
        values = {
            "id": row["id"],
            "rollout_path": row.get("rollout_path", f"/tmp/{row['id']}.jsonl"),
            "created_at": row.get("created_at", 1_784_040_000),
            "updated_at": row.get("updated_at", 1_784_040_060),
            "source": row.get("source", "vscode"),
            "cwd": row.get("cwd", f"/tmp/{row['id']}"),
            "title": row.get("title", "sensitive title"),
            "preview": row.get("preview", "sensitive preview"),
            "first_user_message": row.get("first_user_message", "sensitive prompt"),
            "archived": row.get("archived", 0),
            "git_branch": row.get("git_branch", "arya/test"),
            "git_origin_url": row.get("git_origin_url", "https://github.com/e3-solutions/example.git"),
            "created_at_ms": row.get("created_at_ms", 1_784_040_000_000),
            "updated_at_ms": row.get("updated_at_ms", 1_784_040_060_000),
            "thread_source": row.get("thread_source", "user"),
        }
        connection.execute(
            """
            insert into threads (
                id, rollout_path, created_at, updated_at, source, cwd, title, preview,
                first_user_message, archived, git_branch, git_origin_url,
                created_at_ms, updated_at_ms, thread_source
            ) values (
                :id, :rollout_path, :created_at, :updated_at, :source, :cwd, :title, :preview,
                :first_user_message, :archived, :git_branch, :git_origin_url,
                :created_at_ms, :updated_at_ms, :thread_source
            )
            """,
            values,
        )
    connection.commit()
    connection.close()
    return path


class RecordingUploader:
    def __init__(self):
        self.payloads = []

    def upload_message(self, record, *, base):
        self.payloads.append(json.loads(json.dumps(record)))


def configure_successful_uploads(presence, monkeypatch, uploader):
    monkeypatch.setenv("CODEX_SESSION_LOG_AUTO_UPLOAD", "1")
    monkeypatch.setattr(
        presence.session_logging.IngestUploader,
        "from_env",
        classmethod(lambda cls: uploader),
    )


def test_resident_presence_publishes_user_and_subagent_tasks_with_parent_linkage(tmp_path, monkeypatch):
    now = datetime(2026, 7, 14, 18, 20, tzinfo=timezone.utc)
    updated_ms = int((now - timedelta(seconds=10)).timestamp() * 1000)
    parent_id = "019f02bd-5d00-7e22-8e1a-4a30e7261c9f"
    codex_home = tmp_path / "codex"
    create_native_database(
        codex_home / "state_5.sqlite",
        [
            {"id": parent_id, "updated_at_ms": updated_ms},
            {
                "id": "external",
                "updated_at_ms": updated_ms,
                "git_origin_url": "https://github.com/example/private.git",
            },
            {
                "id": "subagent",
                "updated_at_ms": updated_ms,
                "thread_source": "subagent",
                "source": json.dumps(
                    {
                        "subagent": {
                            "thread_spawn": {
                                "parent_thread_id": parent_id,
                                "depth": 1,
                                "agent_nickname": "private nickname",
                            }
                        }
                    }
                ),
            },
            {"id": "archived", "updated_at_ms": updated_ms, "archived": 1},
        ],
    )
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "logging"))
    presence = load_presence()
    uploader = RecordingUploader()
    configure_successful_uploads(presence, monkeypatch, uploader)

    result = presence.run_presence(
        codex_home=codex_home,
        state_path=tmp_path / "presence.json",
        now=now,
    )
    records = {record["session_id"]: record for record in uploader.payloads}
    details = {
        session_id: json.loads(
            (tmp_path / "logging" / record["local_content_path"]).read_text(encoding="utf-8")
        )
        for session_id, record in records.items()
    }

    assert result["eligible"] == 2
    assert result["published"] == 2
    assert set(records) == {parent_id, "subagent"}
    assert all(record["event_type"] == "resident_presence" for record in records.values())
    assert all(record["seq"] == 0 for record in records.values())
    assert records[parent_id]["id"] == presence.session_logging.sha256_hex(f"resident-presence:{parent_id}")[:32]
    assert details["subagent"]["metadata"] == {
        "cwd": "/tmp/subagent",
        "transcript_path": "/tmp/subagent.jsonl",
        "repo_remote": "https://github.com/e3-solutions/example.git",
        "source": "resident_presence",
        "native_created_at": "2026-07-14T14:40:00+00:00",
        "native_updated_at": "2026-07-14T18:19:50+00:00",
        "git_branch": "arya/test",
        "thread_source": "subagent",
        "parent_thread_id": parent_id,
    }
    serialized = json.dumps({"records": records, "details": details})
    for secret in ("sensitive title", "sensitive preview", "sensitive prompt", "private nickname"):
        assert secret not in serialized
    for forbidden_key in ("title", "preview", "first_user_message", "prompt", "content"):
        assert all(forbidden_key not in detail["metadata"] for detail in details.values())


def test_fresh_subagent_presence_is_published_when_parent_is_idle(tmp_path, monkeypatch):
    now = datetime(2026, 7, 14, 18, 20, tzinfo=timezone.utc)
    parent_id = "019f02bd-5d00-7e22-8e1a-4a30e7261c9f"
    codex_home = tmp_path / "codex"
    create_native_database(
        codex_home / "state_5.sqlite",
        [
            {
                "id": parent_id,
                "updated_at_ms": int((now - timedelta(hours=2)).timestamp() * 1000),
            },
            {
                "id": "active-child",
                "thread_source": "subagent",
                "source": json.dumps(
                    {"subagent": {"thread_spawn": {"parent_thread_id": parent_id, "depth": 1}}}
                ),
                "updated_at_ms": int((now - timedelta(seconds=5)).timestamp() * 1000),
            },
        ],
    )
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "logging"))
    presence = load_presence()
    uploader = RecordingUploader()
    configure_successful_uploads(presence, monkeypatch, uploader)

    result = presence.run_presence(
        codex_home=codex_home,
        state_path=tmp_path / "presence.json",
        now=now,
    )

    assert result["eligible"] == 2
    assert result["published"] == 1
    assert result["drain"]["expired"] == 1
    assert [record["session_id"] for record in uploader.payloads] == ["active-child"]
    assert uploader.payloads[0]["metadata"]["parent_thread_id"] == parent_id


def test_resident_presence_is_deduplicated_and_reuses_one_event_per_task(tmp_path, monkeypatch):
    now = datetime(2026, 7, 14, 18, 20, tzinfo=timezone.utc)
    codex_home = tmp_path / "codex"
    database = create_native_database(
        codex_home / "state_5.sqlite",
        [{"id": "thread-1", "updated_at_ms": int((now - timedelta(seconds=10)).timestamp() * 1000)}],
    )
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "logging"))
    presence = load_presence()
    uploader = RecordingUploader()
    configure_successful_uploads(presence, monkeypatch, uploader)
    state_path = tmp_path / "presence.json"

    first = presence.run_presence(codex_home=codex_home, state_path=state_path, now=now)
    second = presence.run_presence(codex_home=codex_home, state_path=state_path, now=now + timedelta(seconds=30))
    with sqlite3.connect(database) as connection:
        next_updated = int((now + timedelta(seconds=40)).timestamp() * 1000)
        connection.execute("update threads set updated_at_ms = ? where id = 'thread-1'", (next_updated,))
        connection.commit()
    third = presence.run_presence(codex_home=codex_home, state_path=state_path, now=now + timedelta(seconds=50))

    assert first["published"] == 1
    assert second["published"] == 0
    assert third["published"] == 1
    assert [record["id"] for record in uploader.payloads] == [uploader.payloads[0]["id"]] * 2
    assert [record["seq"] for record in uploader.payloads] == [0, 0]
    assert uploader.payloads[1]["created_at"] > uploader.payloads[0]["created_at"]


def test_failed_presence_upload_remains_retryable_without_advancing_state(tmp_path, monkeypatch):
    now = datetime(2026, 7, 14, 18, 20, tzinfo=timezone.utc)
    codex_home = tmp_path / "codex"
    create_native_database(
        codex_home / "state_5.sqlite",
        [{"id": "retry-me", "updated_at_ms": int((now - timedelta(seconds=5)).timestamp() * 1000)}],
    )
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "logging"))
    presence = load_presence()
    monkeypatch.setenv("CODEX_SESSION_LOG_AUTO_UPLOAD", "1")

    class FailingUploader:
        def upload_message(self, record, *, base):
            raise RuntimeError("offline")

    monkeypatch.setattr(
        presence.session_logging.IngestUploader,
        "from_env",
        classmethod(lambda cls: FailingUploader()),
    )
    state_path = tmp_path / "presence.json"
    failed = presence.run_presence(codex_home=codex_home, state_path=state_path, now=now)
    failed_state = json.loads(state_path.read_text(encoding="utf-8"))

    uploader = RecordingUploader()
    configure_successful_uploads(presence, monkeypatch, uploader)
    retried = presence.run_presence(
        codex_home=codex_home,
        state_path=state_path,
        now=now + timedelta(seconds=30),
    )

    assert failed["published"] == 0
    assert failed["failed"] == 1
    assert "retry-me" not in failed_state["published"]
    assert retried["published"] == 1
    assert len(uploader.payloads) == 1


def test_transient_outage_stops_after_one_attempt_and_preserves_all_presence_records(tmp_path, monkeypatch):
    now = datetime(2026, 7, 14, 18, 20, tzinfo=timezone.utc)
    codex_home = tmp_path / "codex"
    create_native_database(
        codex_home / "state_5.sqlite",
        [
            {"id": f"offline-{index}", "updated_at_ms": int((now - timedelta(seconds=index)).timestamp() * 1000)}
            for index in range(3)
        ],
    )
    logging_root = tmp_path / "logging"
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(logging_root))
    monkeypatch.setenv("CODEX_SESSION_LOG_AUTO_UPLOAD", "1")
    presence = load_presence()
    attempts = []

    class OfflineUploader:
        def upload_message(self, record, *, base):
            attempts.append(record["id"])
            raise TimeoutError("offline")

    monkeypatch.setattr(
        presence.session_logging.IngestUploader,
        "from_env",
        classmethod(lambda cls: OfflineUploader()),
    )

    result = presence.run_presence(
        codex_home=codex_home,
        state_path=tmp_path / "presence.json",
        now=now,
    )

    assert len(attempts) == 1
    assert result["published"] == 0
    assert result["drain"]["failed"] == 1
    assert result["drain"]["remaining"] == 3
    assert len(list((logging_root / "presence-queue" / "pending").glob("*.json"))) == 3


def test_presence_skips_newest_incompatible_database_and_uses_valid_fallback(tmp_path, monkeypatch):
    now = datetime(2026, 7, 14, 18, 20, tzinfo=timezone.utc)
    codex_home = tmp_path / "codex"
    valid = create_native_database(
        codex_home / "sqlite" / "state_4.sqlite",
        [{"id": "fallback", "updated_at_ms": int((now - timedelta(seconds=5)).timestamp() * 1000)}],
    )
    invalid = create_native_database(codex_home / "state_5.sqlite", [], valid=False)
    os.utime(valid, (1, 1))
    os.utime(invalid, (2, 2))
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "logging"))
    presence = load_presence()
    uploader = RecordingUploader()
    configure_successful_uploads(presence, monkeypatch, uploader)

    result = presence.run_presence(
        codex_home=codex_home,
        state_path=tmp_path / "presence.json",
        now=now,
    )

    assert result["database"] == str(valid.resolve())
    assert result["published"] == 1


def test_presence_database_selection_counts_wal_activity(tmp_path, monkeypatch):
    now = datetime(2026, 7, 14, 18, 20, tzinfo=timezone.utc)
    codex_home = tmp_path / "codex"
    active = create_native_database(
        codex_home / "state_5.sqlite",
        [{"id": "active", "updated_at_ms": int((now - timedelta(seconds=5)).timestamp() * 1000)}],
    )
    legacy = create_native_database(
        codex_home / "state_4.sqlite",
        [{"id": "legacy", "updated_at_ms": int((now - timedelta(minutes=5)).timestamp() * 1000)}],
    )
    connection = sqlite3.connect(active)
    connection.execute("pragma journal_mode = wal")
    connection.execute(
        "update threads set updated_at_ms = ? where id = 'active'",
        (int((now - timedelta(seconds=1)).timestamp() * 1000),),
    )
    connection.commit()
    wal = Path(f"{active}-wal")
    assert wal.exists()
    os.utime(active, (1, 1))
    os.utime(legacy, (2, 2))
    os.utime(wal, (3, 3))
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "logging"))
    presence = load_presence()
    uploader = RecordingUploader()
    configure_successful_uploads(presence, monkeypatch, uploader)
    try:
        result = presence.run_presence(
            codex_home=codex_home,
            state_path=tmp_path / "presence.json",
            now=now,
        )
    finally:
        connection.close()

    assert result["database"] == str(active.resolve())
    assert {record["session_id"] for record in uploader.payloads} == {"active", "legacy"}


def test_presence_merges_tasks_from_all_compatible_native_databases(tmp_path, monkeypatch):
    now = datetime(2026, 7, 14, 18, 20, tzinfo=timezone.utc)
    codex_home = tmp_path / "codex"
    older = create_native_database(
        codex_home / "state_4.sqlite",
        [
            {"id": "older-process", "updated_at_ms": int((now - timedelta(seconds=10)).timestamp() * 1000)},
            {
                "id": "shared-thread",
                "git_branch": "arya/old",
                "updated_at_ms": int((now - timedelta(seconds=20)).timestamp() * 1000),
            },
        ],
    )
    newer = create_native_database(
        codex_home / "state_5.sqlite",
        [
            {"id": "newer-process", "updated_at_ms": int((now - timedelta(seconds=5)).timestamp() * 1000)},
            {
                "id": "shared-thread",
                "git_branch": "arya/new",
                "updated_at_ms": int((now - timedelta(seconds=2)).timestamp() * 1000),
            },
        ],
    )
    os.utime(older, (1, 1))
    os.utime(newer, (2, 2))
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "logging"))
    presence = load_presence()
    uploader = RecordingUploader()
    configure_successful_uploads(presence, monkeypatch, uploader)

    result = presence.run_presence(
        codex_home=codex_home,
        state_path=tmp_path / "presence.json",
        now=now,
    )
    records = {record["session_id"]: record for record in uploader.payloads}

    assert result["database"] == str(newer.resolve())
    assert set(records) == {"older-process", "newer-process", "shared-thread"}
    assert records["shared-thread"]["metadata"]["git_branch"] == "arya/new"


def test_presence_does_not_resurrect_archived_thread_from_older_database(tmp_path, monkeypatch):
    now = datetime(2026, 7, 14, 18, 20, tzinfo=timezone.utc)
    codex_home = tmp_path / "codex"
    older = create_native_database(
        codex_home / "state_4.sqlite",
        [
            {
                "id": "shared-thread",
                "archived": 0,
                "updated_at_ms": int((now - timedelta(seconds=2)).timestamp() * 1000),
            }
        ],
    )
    newer = create_native_database(
        codex_home / "state_5.sqlite",
        [
            {
                "id": "shared-thread",
                "archived": 1,
                "updated_at_ms": int((now - timedelta(seconds=2)).timestamp() * 1000),
            }
        ],
    )
    os.utime(older, (2, 2))
    os.utime(newer, (1, 1))
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "logging"))
    presence = load_presence()
    uploader = RecordingUploader()
    configure_successful_uploads(presence, monkeypatch, uploader)

    result = presence.run_presence(
        codex_home=codex_home,
        state_path=tmp_path / "presence.json",
        now=now,
    )

    assert result["eligible"] == 0
    assert result["published"] == 0
    assert uploader.payloads == []


def test_presence_limit_prioritizes_newest_tasks(tmp_path, monkeypatch):
    now = datetime(2026, 7, 14, 18, 20, tzinfo=timezone.utc)
    codex_home = tmp_path / "codex"
    rows = [
        {
            "id": f"thread-{index}",
            "updated_at_ms": int((now - timedelta(minutes=10 - index)).timestamp() * 1000),
        }
        for index in range(10)
    ]
    create_native_database(codex_home / "state_5.sqlite", rows)
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "logging"))
    presence = load_presence()
    uploader = RecordingUploader()
    configure_successful_uploads(presence, monkeypatch, uploader)

    result = presence.run_presence(
        codex_home=codex_home,
        state_path=tmp_path / "presence.json",
        now=now,
        limit=3,
    )

    assert result["published"] == 3
    assert {record["session_id"] for record in uploader.payloads} == {"thread-7", "thread-8", "thread-9"}


def test_presence_limit_is_applied_after_eligibility(tmp_path, monkeypatch):
    now = datetime(2026, 7, 14, 18, 20, tzinfo=timezone.utc)
    codex_home = tmp_path / "codex"
    rows = [
        {
            "id": f"external-{index}",
            "updated_at_ms": int((now - timedelta(seconds=index)).timestamp() * 1000),
            "git_origin_url": "https://github.com/example/private.git",
        }
        for index in range(120)
    ]
    rows.append(
        {
            "id": "eligible-behind-window",
            "updated_at_ms": int((now - timedelta(minutes=3)).timestamp() * 1000),
        }
    )
    create_native_database(codex_home / "state_5.sqlite", rows)
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "logging"))
    presence = load_presence()
    uploader = RecordingUploader()
    configure_successful_uploads(presence, monkeypatch, uploader)

    result = presence.run_presence(
        codex_home=codex_home,
        state_path=tmp_path / "presence.json",
        now=now,
        limit=1,
    )

    assert result["published"] == 1
    assert [record["session_id"] for record in uploader.payloads] == ["eligible-behind-window"]


def test_presence_accepts_empty_legacy_thread_source(tmp_path, monkeypatch):
    now = datetime(2026, 7, 14, 18, 20, tzinfo=timezone.utc)
    codex_home = tmp_path / "codex"
    create_native_database(
        codex_home / "state_5.sqlite",
        [
            {
                "id": "legacy-empty-source",
                "thread_source": "",
                "updated_at_ms": int((now - timedelta(seconds=5)).timestamp() * 1000),
            }
        ],
    )
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "logging"))
    presence = load_presence()
    uploader = RecordingUploader()
    configure_successful_uploads(presence, monkeypatch, uploader)

    result = presence.run_presence(
        codex_home=codex_home,
        state_path=tmp_path / "presence.json",
        now=now,
    )

    assert result["published"] == 1
    assert [record["session_id"] for record in uploader.payloads] == ["legacy-empty-source"]


def test_stale_queued_presence_expires_instead_of_creating_false_activity(tmp_path, monkeypatch):
    now = datetime(2026, 7, 14, 18, 20, tzinfo=timezone.utc)
    codex_home = tmp_path / "codex"
    create_native_database(
        codex_home / "state_5.sqlite",
        [
            {
                "id": "stale-task",
                "updated_at_ms": int((now - timedelta(minutes=10)).timestamp() * 1000),
            }
        ],
    )
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "logging"))
    presence = load_presence()
    uploader = RecordingUploader()
    configure_successful_uploads(presence, monkeypatch, uploader)
    state_path = tmp_path / "presence.json"

    result = presence.run_presence(
        codex_home=codex_home,
        state_path=state_path,
        now=now,
    )

    assert result["published"] == 0
    assert result["failed"] == 0
    assert result["drain"]["expired"] == 1
    assert uploader.payloads == []
    assert "stale-task" in json.loads(state_path.read_text(encoding="utf-8"))["published"]


def test_presence_honors_upload_opt_out_without_reading_native_state(tmp_path, monkeypatch):
    presence = load_presence()
    monkeypatch.setenv("CODEX_SESSION_LOG_AUTO_UPLOAD", "0")
    monkeypatch.setattr(
        presence,
        "discover_recent_threads",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("must not read database")),
    )

    result = presence.run_presence(
        codex_home=tmp_path / "missing",
        state_path=tmp_path / "presence.json",
    )

    assert result == {"disabled": True, "published": 0, "queued": 0}
    assert json.loads((tmp_path / "presence.json").read_text(encoding="utf-8"))["last_result"] == "disabled"


def test_presence_upload_opt_out_persists_without_environment(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "custom-state"))
    monkeypatch.setenv("CODEX_SESSION_LOG_AUTO_UPLOAD", "0")
    presence = load_presence()

    assert presence.session_logging.auto_upload_enabled() is False
    preference = tmp_path / "codex" / "session-logging" / "preferences.json"
    assert json.loads(preference.read_text(encoding="utf-8")) == {"enabled": False}
    assert preference.stat().st_mode & 0o777 == 0o600
    monkeypatch.delenv("CODEX_SESSION_LOG_AUTO_UPLOAD")
    assert presence.session_logging.auto_upload_enabled() is False
    monkeypatch.setenv("CODEX_SESSION_LOG_AUTO_UPLOAD", "1")
    assert presence.session_logging.auto_upload_enabled() is True
    monkeypatch.delenv("CODEX_SESSION_LOG_AUTO_UPLOAD")
    assert presence.session_logging.auto_upload_enabled() is True


def test_presence_supports_older_native_schema_by_resolving_repo_from_cwd(tmp_path, monkeypatch):
    now = datetime(2026, 7, 14, 18, 20, tzinfo=timezone.utc)
    codex_home = tmp_path / "codex"
    database = codex_home / "state_3.sqlite"
    database.parent.mkdir(parents=True)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "create table threads (id text primary key, rollout_path text, cwd text, created_at integer, updated_at integer)"
        )
        connection.execute(
            "insert into threads values (?, ?, ?, ?, ?)",
            (
                "legacy",
                "/tmp/legacy.jsonl",
                "/tmp/legacy-repo",
                int((now - timedelta(minutes=1)).timestamp()),
                int((now - timedelta(seconds=5)).timestamp()),
            ),
        )
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "logging"))
    presence = load_presence()
    monkeypatch.setattr(
        presence.session_logging,
        "git_origin_remote",
        lambda cwd: "git@github.com:e3-solutions/legacy.git",
    )
    uploader = RecordingUploader()
    configure_successful_uploads(presence, monkeypatch, uploader)

    result = presence.run_presence(
        codex_home=codex_home,
        state_path=tmp_path / "presence.json",
        now=now,
    )

    assert result["published"] == 1
    assert uploader.payloads[0]["metadata"]["repo_remote"] == "git@github.com:e3-solutions/legacy.git"


def test_presence_uses_its_own_queue_and_dead_letters_permanent_failures(tmp_path, monkeypatch):
    now = datetime(2026, 7, 14, 18, 20, tzinfo=timezone.utc)
    codex_home = tmp_path / "codex"
    create_native_database(
        codex_home / "state_5.sqlite",
        [{"id": "permanent", "updated_at_ms": int((now - timedelta(seconds=5)).timestamp() * 1000)}],
    )
    logging_root = tmp_path / "logging"
    unrelated = logging_root / "queue" / "pending" / "unrelated.json"
    unrelated.parent.mkdir(parents=True)
    unrelated.write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(logging_root))
    monkeypatch.setenv("CODEX_SESSION_LOG_AUTO_UPLOAD", "1")
    presence = load_presence()

    class RejectingUploader:
        def upload_message(self, record, *, base):
            raise presence.session_logging.PermanentUploadError("invalid", status=400)

    monkeypatch.setattr(
        presence.session_logging.IngestUploader,
        "from_env",
        classmethod(lambda cls: RejectingUploader()),
    )
    state_path = tmp_path / "presence.json"

    result = presence.run_presence(codex_home=codex_home, state_path=state_path, now=now)

    assert result["published"] == 0
    assert result["drain"]["dead_lettered"] == 1
    assert unrelated.exists()
    assert len(list((logging_root / "presence-queue" / "dead-letter").glob("*.json"))) == 1
    assert not list((logging_root / "presence-queue" / "pending").glob("*.json"))
    assert state_path.stat().st_mode & 0o777 == 0o600

    uploader = RecordingUploader()
    configure_successful_uploads(presence, monkeypatch, uploader)
    recovered = presence.run_presence(
        codex_home=codex_home,
        state_path=state_path,
        now=now + timedelta(seconds=30),
    )

    assert recovered["published"] == 1
    assert not list((logging_root / "presence-queue" / "dead-letter").glob("*.json"))


def test_presence_lock_prevents_overlapping_publishers(tmp_path, monkeypatch):
    presence = load_presence()
    monkeypatch.setenv("CODEX_SESSION_LOG_AUTO_UPLOAD", "1")
    state_path = tmp_path / "presence.json"
    lock_path = state_path.with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = presence.run_presence(codex_home=tmp_path / "codex", state_path=state_path)

    assert result == {"locked": True, "published": 0, "queued": 0}


def test_presence_cli_posts_real_ingest_payload_to_loopback_endpoint(tmp_path):
    now = datetime.now(timezone.utc)
    codex_home = tmp_path / "codex"
    create_native_database(
        codex_home / "state_5.sqlite",
        [{"id": "live-cli", "updated_at_ms": int((now - timedelta(seconds=2)).timestamp() * 1000)}],
    )
    received = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - BaseHTTPRequestHandler API.
            length = int(self.headers["content-length"])
            received.append(json.loads(self.rfile.read(length)))
            self.send_response(200)
            self.end_headers()

        def log_message(self, _format, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        completed = subprocess.run(
            [
                sys.executable,
                str(SCRIPTS / "publish_presence.py"),
                "--codex-home",
                str(codex_home),
                "--state-path",
                str(tmp_path / "presence.json"),
            ],
            env={
                **os.environ,
                "CODEX_SESSION_LOG_AUTO_UPLOAD": "1",
                "CODEX_SESSION_LOG_STATE_DIR": str(tmp_path / "logging"),
                "CODEX_SESSION_LOG_INGEST_URL": f"http://127.0.0.1:{server.server_port}/ingest",
            },
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    result = json.loads(completed.stdout)
    assert completed.returncode == 0, completed.stderr
    assert result["published"] == 1
    assert len(received) == 1
    payload = received[0]
    assert payload["plugin"] == {"name": "codex-session-logging", "version": "0.2.6"}
    assert payload["record"]["session_id"] == "live-cli"
    assert payload["event"]["event_type"] == "resident_presence"
    assert "sensitive prompt" not in json.dumps(payload)
