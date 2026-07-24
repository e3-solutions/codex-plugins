from __future__ import annotations

import base64
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from uuid import UUID

import pytest


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "plugins" / "codex-session-logging" / "scripts"
PARENT_ID = "019f02bd-5d00-7e22-8e1a-4a30e7261c9f"
CHILD_ID = "019f02bd-5d00-7e22-8e1a-4a30e7261ca0"
GRANDCHILD_ID = "019f02bd-5d00-7e22-8e1a-4a30e7261ca1"


@pytest.fixture(autouse=True)
def isolated_state(tmp_path, monkeypatch):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "logging"))
    monkeypatch.setenv("CODEX_SESSION_LOG_AUTO_UPLOAD", "0")
    monkeypatch.setenv("CODEX_SESSION_ROLLOUT_INITIAL_LOOKBACK_SECONDS", "2000000000")


def load_rollout_sync():
    sys.path.insert(0, str(SCRIPTS))
    try:
        for name in ("session_logging", "publish_presence", "rollout_sync"):
            sys.modules.pop(name, None)
        spec = importlib.util.spec_from_file_location("rollout_sync", SCRIPTS / "rollout_sync.py")
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        return module
    finally:
        sys.path.remove(str(SCRIPTS))


def write_rollout(
    path: Path,
    *,
    session_id: str,
    parent_id: str | None = None,
    source: object = "vscode",
    records: list[dict] | None = None,
) -> bytes:
    payload = {
        "id": session_id,
        "session_id": parent_id or session_id,
        "source": source,
    }
    if parent_id:
        payload["parent_thread_id"] = parent_id
    items = [{"type": "session_meta", "payload": payload}, *(records or [])]
    raw = b"".join(json.dumps(item, separators=(",", ":")).encode() + b"\n" for item in items)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return raw


def create_database(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path)
    connection.execute(
        """
        create table threads (
            id text primary key,
            rollout_path text not null,
            created_at integer not null,
            updated_at integer not null,
            source text not null,
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
    for index, row in enumerate(rows):
        values = {
            "id": row["id"],
            "rollout_path": str(row["rollout_path"]),
            "created_at": 1,
            "updated_at": index + 1,
            "source": row.get("source", "vscode"),
            "cwd": row.get("cwd", str(path.parent)),
            "archived": row.get("archived", 0),
            "git_branch": "arya/test",
            "git_origin_url": row.get(
                "git_origin_url", "https://github.com/e3-solutions/codex-plugins.git"
            ),
            "created_at_ms": 1000,
            "updated_at_ms": 1000 + index,
            "thread_source": row.get("thread_source", "user"),
        }
        connection.execute(
            """
            insert into threads (
                id, rollout_path, created_at, updated_at, source, cwd, archived,
                git_branch, git_origin_url, created_at_ms, updated_at_ms, thread_source
            ) values (
                :id, :rollout_path, :created_at, :updated_at, :source, :cwd, :archived,
                :git_branch, :git_origin_url, :created_at_ms, :updated_at_ms, :thread_source
            )
            """,
            values,
        )
    connection.commit()
    connection.close()


def queue_records(state: Path) -> list[dict]:
    return [
        json.loads(path.read_text(encoding="utf-8"))
        for path in sorted((state / "queue" / "pending").glob("*.json"))
    ]


def decoded_chunks(module, state: Path, records: list[dict]) -> bytes:
    ordered = sorted(records, key=lambda item: item["metadata"]["start_offset"])
    return b"".join(
        base64.b64decode(
            json.loads((state / record["local_content_path"]).read_text(encoding="utf-8"))[
                "content_base64"
            ]
        )
        for record in ordered
    )


def test_sync_captures_exact_parent_and_subagent_rollout_bytes_once(tmp_path):
    codex_home = tmp_path / "codex"
    parent_path = tmp_path / "rollouts" / "parent.jsonl"
    child_path = tmp_path / "rollouts" / "child.jsonl"
    parent_raw = write_rollout(
        parent_path,
        session_id=PARENT_ID,
        records=[{"type": "response_item", "payload": {"role": "user", "content": "secret"}}],
    )
    child_raw = write_rollout(
        child_path,
        session_id=CHILD_ID,
        parent_id=PARENT_ID,
        source={"subagent": {"other": "guardian"}},
        records=[
            {"type": "response_item", "payload": {"role": "assistant", "content": "answer"}},
            {"type": "event_msg", "payload": {"type": "tool_output", "output": "full output"}},
        ],
    )
    create_database(
        codex_home / "state_5.sqlite",
        [
            {"id": PARENT_ID, "rollout_path": parent_path},
            {
                "id": CHILD_ID,
                "rollout_path": child_path,
                "thread_source": "subagent",
            },
        ],
    )
    module = load_rollout_sync()

    first = module.sync_rollouts(codex_home=codex_home)
    second = module.sync_rollouts(codex_home=codex_home)
    state = tmp_path / "logging"
    records = queue_records(state)
    by_session = {record["session_id"]: record for record in records}

    assert first == {"queued": 2, "eligible": 2, "errors": []}
    assert second == {"queued": 0, "eligible": 0, "errors": []}
    assert decoded_chunks(module, state, [by_session[PARENT_ID]]) == parent_raw
    assert decoded_chunks(module, state, [by_session[CHILD_ID]]) == child_raw
    assert by_session[CHILD_ID]["metadata"]["parent_thread_id"] == PARENT_ID
    assert by_session[CHILD_ID]["metadata"]["root_thread_id"] == PARENT_ID
    assert by_session[CHILD_ID]["metadata"]["rollout_source_category"] == "subagent.guardian"
    assert by_session[CHILD_ID]["thread_id"] == module.session_logging.sha256_hex(str(child_path))
    assert by_session[CHILD_ID]["created_at"] == "1970-01-01T00:00:01+00:00"
    ingest_payload = module.session_logging.build_ingest_payload(
        by_session[CHILD_ID],
        base=state,
    )
    assert ingest_payload["kind"] == "rollout_chunk"
    assert ingest_payload["record"]["type"] == "event"
    assert base64.b64decode(ingest_payload["rollout_chunk"]["content_base64"]) == child_raw
    assert "secret" not in json.dumps(by_session[PARENT_ID])
    assert "full output" not in json.dumps(by_session[CHILD_ID])


def test_sync_captures_unterminated_tail_and_continues_from_exact_offset(tmp_path):
    codex_home = tmp_path / "codex"
    rollout = tmp_path / "rollout.jsonl"
    complete = write_rollout(rollout, session_id=PARENT_ID)
    rollout.write_bytes(complete + b'{"type":"event_msg"')
    create_database(codex_home / "state.sqlite", [{"id": PARENT_ID, "rollout_path": rollout}])
    module = load_rollout_sync()

    module.sync_rollouts(codex_home=codex_home)
    first_records = queue_records(tmp_path / "logging")
    partial = b'{"type":"event_msg"'
    assert decoded_chunks(module, tmp_path / "logging", first_records) == complete + partial

    suffix = b',"payload":{"type":"done"}}\n'
    with rollout.open("ab") as handle:
        handle.write(suffix)
    result = module.sync_rollouts(
        codex_home=codex_home,
        hook_payload={"session_id": PARENT_ID, "transcript_path": str(rollout)},
    )
    records = queue_records(tmp_path / "logging")

    assert result["queued"] == 1
    assert decoded_chunks(module, tmp_path / "logging", records) == complete + partial + suffix


def test_sync_retries_same_deterministic_chunk_after_crash_before_checkpoint(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    rollout = tmp_path / "rollout.jsonl"
    write_rollout(rollout, session_id=PARENT_ID)
    create_database(codex_home / "state.sqlite", [{"id": PARENT_ID, "rollout_path": rollout}])
    module = load_rollout_sync()
    original_enqueue = module.session_logging.enqueue_record
    crashed_ids = []

    def enqueue_then_crash(base, record):
        original_enqueue(base, record)
        crashed_ids.append(record["id"])
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(module.session_logging, "enqueue_record", enqueue_then_crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        module.sync_rollouts(codex_home=codex_home)
    monkeypatch.setattr(module.session_logging, "enqueue_record", original_enqueue)

    result = module.sync_rollouts(
        codex_home=codex_home,
        hook_payload={"session_id": PARENT_ID, "transcript_path": str(rollout)},
    )
    records = queue_records(tmp_path / "logging")

    assert result["queued"] == 1
    assert [record["id"] for record in records] == crashed_ids


def test_sync_detects_replaced_rollout_as_a_new_generation(tmp_path):
    codex_home = tmp_path / "codex"
    rollout = tmp_path / "rollout.jsonl"
    first_bytes = write_rollout(rollout, session_id=PARENT_ID)
    create_database(codex_home / "state.sqlite", [{"id": PARENT_ID, "rollout_path": rollout}])
    module = load_rollout_sync()
    module.sync_rollouts(codex_home=codex_home)
    first_record = queue_records(tmp_path / "logging")[0]

    replacement = tmp_path / "replacement.jsonl"
    second_bytes = write_rollout(
        replacement,
        session_id=PARENT_ID,
        records=[{"type": "event_msg", "payload": {"replacement": True}}],
    )
    replacement.replace(rollout)
    result = module.sync_rollouts(
        codex_home=codex_home,
        hook_payload={"session_id": PARENT_ID, "transcript_path": str(rollout)},
    )
    records = queue_records(tmp_path / "logging")
    second_record = next(item for item in records if item["id"] != first_record["id"])

    assert result["queued"] == 1
    assert first_record["metadata"]["file_generation"] != second_record["metadata"]["file_generation"]
    assert decoded_chunks(module, tmp_path / "logging", [first_record]) == first_bytes
    assert decoded_chunks(module, tmp_path / "logging", [second_record]) == second_bytes


def test_hook_trigger_matrix_skips_ordinary_tools_and_catches_agent_coordination(monkeypatch):
    module = load_rollout_sync()
    calls = []
    monkeypatch.setattr(
        module,
        "sync_rollouts",
        lambda **_kwargs: calls.append(True) or {"queued": 3},
    )

    skipped = module.sync_after_hook(
        {"tool_name": "functions.exec_command"},
        event_name="PostToolUse",
    )
    captured = module.sync_after_hook(
        {"tool_name": "functions.collaboration.spawn_agent"},
        event_name="PostToolUse",
    )
    recovered = module.sync_after_hook({}, event_name="UserPromptSubmit")

    assert skipped == {"queued": 0, "skipped": "ordinary_tool"}
    assert captured == {"queued": 3}
    assert recovered == {"queued": 3}
    assert len(calls) == 2


def test_rollout_chunk_http_400_is_retained_for_server_rollout_compatibility(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    rollout = tmp_path / "rollout.jsonl"
    write_rollout(rollout, session_id=PARENT_ID)
    create_database(codex_home / "state.sqlite", [{"id": PARENT_ID, "rollout_path": rollout}])
    module = load_rollout_sync()
    module.sync_rollouts(codex_home=codex_home)
    state = tmp_path / "logging"
    record = queue_records(state)[0]
    uploader = module.session_logging.IngestUploader(url="https://example.test")

    def reject(_payload):
        raise module.session_logging.PermanentUploadError(
            "old server: event must be an object",
            status=400,
        )

    monkeypatch.setattr(uploader, "post", reject)

    with pytest.raises(RuntimeError, match="old server"):
        uploader.upload_message(record, base=state)

    def reject_invalid(_payload):
        raise module.session_logging.PermanentUploadError(
            "content hash mismatch",
            status=400,
        )

    monkeypatch.setattr(uploader, "post", reject_invalid)
    with pytest.raises(module.session_logging.PermanentUploadError, match="hash mismatch"):
        uploader.upload_message(record, base=state)


def test_sqlite_thread_spawn_parent_is_fallback_when_session_meta_omits_it(tmp_path):
    codex_home = tmp_path / "codex"
    rollout = tmp_path / "rollout.jsonl"
    write_rollout(
        rollout,
        session_id=CHILD_ID,
        source={"subagent": {"thread_spawn": {}}},
    )
    create_database(
        codex_home / "state.sqlite",
        [{
            "id": CHILD_ID,
            "rollout_path": rollout,
            "thread_source": "subagent",
            "source": json.dumps({
                "subagent": {"thread_spawn": {"parent_thread_id": PARENT_ID}}
            }),
        }],
    )
    module = load_rollout_sync()

    module.sync_rollouts(codex_home=codex_home)
    record = queue_records(tmp_path / "logging")[0]

    assert record["metadata"]["parent_thread_id"] == PARENT_ID
    assert record["metadata"]["root_thread_id"] == PARENT_ID


def test_row_seen_before_rollout_exists_remains_pending_until_a_later_hook(tmp_path):
    codex_home = tmp_path / "codex"
    rollout = tmp_path / "late-rollout.jsonl"
    create_database(codex_home / "state.sqlite", [{"id": PARENT_ID, "rollout_path": rollout}])
    module = load_rollout_sync()

    first = module.sync_rollouts(codex_home=codex_home)
    pending_state = json.loads(
        (tmp_path / "logging" / "rollout-sync" / "state.json").read_text(encoding="utf-8")
    )
    raw = write_rollout(rollout, session_id=PARENT_ID)
    second = module.sync_rollouts(codex_home=codex_home)
    records = queue_records(tmp_path / "logging")

    assert first == {"queued": 0, "eligible": 0, "errors": []}
    assert PARENT_ID in pending_state["pending_rows"]
    assert second == {"queued": 1, "eligible": 1, "errors": []}
    assert decoded_chunks(module, tmp_path / "logging", records) == raw


def test_large_baseline_is_bounded_and_resumes_from_pending_cursor(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    rollout = tmp_path / "large-rollout.jsonl"
    raw = write_rollout(
        rollout,
        session_id=PARENT_ID,
        records=[{"type": "event_msg", "payload": {"data": "x" * 600}}],
    )
    create_database(codex_home / "state.sqlite", [{"id": PARENT_ID, "rollout_path": rollout}])
    module = load_rollout_sync()
    monkeypatch.setattr(module, "MAX_SYNC_BYTES_PER_HOOK", 200)
    monkeypatch.setattr(module, "MAX_CHUNK_BYTES", 100)

    first = module.sync_rollouts(codex_home=codex_home)
    first_records = queue_records(tmp_path / "logging")
    second = module.sync_rollouts(codex_home=codex_home)
    second_records = queue_records(tmp_path / "logging")

    assert first["queued"] == 2
    assert len(decoded_chunks(module, tmp_path / "logging", first_records)) == 200
    assert second["queued"] == 2
    assert decoded_chunks(module, tmp_path / "logging", second_records) == raw[:400]


def test_tracked_subagents_beyond_file_budget_are_revisited_round_robin(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    rows = []
    paths = {}
    for index in range(6):
        child_id = str(UUID(int=index + 100))
        path = tmp_path / f"child-{index}.jsonl"
        write_rollout(path, session_id=child_id, parent_id=PARENT_ID)
        paths[child_id] = path
        rows.append({"id": child_id, "rollout_path": path, "thread_source": "subagent"})
    create_database(codex_home / "state.sqlite", rows)
    module = load_rollout_sync()
    monkeypatch.setattr(module, "MAX_FILES_PER_HOOK", 4)

    module.sync_rollouts(codex_home=codex_home)
    module.sync_rollouts(codex_home=codex_home)
    state = json.loads(
        (tmp_path / "logging" / "rollout-sync" / "state.json").read_text(encoding="utf-8")
    )
    target_id = list(state["files"])[5]
    with paths[target_id].open("ab") as handle:
        handle.write(b'{"type":"event_msg","payload":{"late":true}}\n')

    first = module.sync_rollouts(
        codex_home=codex_home,
        hook_payload={"session_id": PARENT_ID},
    )
    second = module.sync_rollouts(
        codex_home=codex_home,
        hook_payload={"session_id": PARENT_ID},
    )

    assert first["queued"] == 0
    assert second["queued"] == 1


def test_missing_pending_rows_rotate_without_starving_older_ready_row(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    ready_id = str(UUID(int=500))
    ready_path = tmp_path / "ready.jsonl"
    raw = write_rollout(ready_path, session_id=ready_id)
    rows = [
        {"id": ready_id, "rollout_path": ready_path},
        {"id": str(UUID(int=501)), "rollout_path": tmp_path / "missing-1.jsonl"},
        {"id": str(UUID(int=502)), "rollout_path": tmp_path / "missing-2.jsonl"},
    ]
    create_database(codex_home / "state.sqlite", rows)
    module = load_rollout_sync()
    monkeypatch.setattr(module, "MAX_PENDING_CHECKS_PER_HOOK", 2)

    first = module.sync_rollouts(codex_home=codex_home)
    second = module.sync_rollouts(codex_home=codex_home)
    records = queue_records(tmp_path / "logging")

    assert first["queued"] == 0
    assert second["queued"] == 1
    assert decoded_chunks(module, tmp_path / "logging", records) == raw


def test_large_tracked_rollout_cannot_consume_pending_subagent_byte_budget(tmp_path, monkeypatch):
    codex_home = tmp_path / "codex"
    children = [str(UUID(int=700)), str(UUID(int=701))]
    paths = {}
    rows = []
    for child_id in children:
        path = tmp_path / f"{child_id}.jsonl"
        write_rollout(path, session_id=child_id, parent_id=PARENT_ID)
        paths[child_id] = path
        rows.append({"id": child_id, "rollout_path": path, "thread_source": "subagent"})
    create_database(codex_home / "state.sqlite", rows)
    module = load_rollout_sync()
    monkeypatch.setattr(module, "MAX_FILES_PER_HOOK", 1)
    module.sync_rollouts(codex_home=codex_home)
    state = json.loads(
        (tmp_path / "logging" / "rollout-sync" / "state.json").read_text(encoding="utf-8")
    )
    tracked_id = next(iter(state["files"]))
    pending_id = next(iter(state["pending_rows"]))
    with paths[tracked_id].open("ab") as handle:
        handle.write(b"x" * 200)
    before_ids = {record["id"] for record in queue_records(tmp_path / "logging")}
    monkeypatch.setattr(module, "MAX_FILES_PER_HOOK", 2)
    monkeypatch.setattr(module, "MAX_SYNC_BYTES_PER_HOOK", 100)

    result = module.sync_rollouts(
        codex_home=codex_home,
        hook_payload={"session_id": PARENT_ID},
    )
    added = [
        record
        for record in queue_records(tmp_path / "logging")
        if record["id"] not in before_ids
    ]

    assert result["queued"] == 2
    assert {record["session_id"] for record in added} == {tracked_id, pending_id}
    assert {record["metadata"]["content_byte_size"] for record in added} == {50}
