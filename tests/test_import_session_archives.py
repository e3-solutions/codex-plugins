from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "import_session_archives.py"


def load_importer():
    spec = importlib.util.spec_from_file_location("import_session_archives", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_codex_record_extracts_query_fields_and_preserves_payload():
    importer = load_importer()
    source = {
        "timestamp": "2026-07-10T12:34:56Z",
        "type": "response_item",
        "payload": {
            "type": "function_call",
            "name": "exec_command",
            "arguments": "{\"cmd\":\"git status\"}",
        },
    }
    row = importer.archive_record_row(
        raw_line=(json.dumps(source) + "\n").encode(),
        seq=7,
        transcript_id="00000000-0000-0000-0000-000000000001",
        import_id="00000000-0000-0000-0000-000000000002",
        user_id="00000000-0000-0000-0000-000000000003",
        platform="codex",
        session_id="session-1",
    )

    assert row["record_type"] == "response_item"
    assert row["record_subtype"] == "function_call"
    assert row["tool_name"] == "exec_command"
    assert "git status" in row["content_excerpt"]
    assert row["payload"] == source
    assert row["raw_text"] is None


def test_claude_record_extracts_tool_and_role():
    importer = load_importer()
    source = {
        "timestamp": "2026-07-10T12:34:56Z",
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "name": "Bash", "input": {"command": "pwd"}}],
        },
    }
    row = importer.archive_record_row(
        raw_line=json.dumps(source).encode(),
        seq=1,
        transcript_id="00000000-0000-0000-0000-000000000001",
        import_id="00000000-0000-0000-0000-000000000002",
        user_id="00000000-0000-0000-0000-000000000003",
        platform="claude",
        session_id="session-2",
    )

    assert row["role"] == "assistant"
    assert row["tool_name"] == "Bash"
    assert "pwd" in row["content_excerpt"]


def test_invalid_json_is_preserved_for_later_repair():
    importer = load_importer()
    row = importer.archive_record_row(
        raw_line=b'{"broken": "quote\\"}\n',
        seq=2,
        transcript_id="00000000-0000-0000-0000-000000000001",
        import_id="00000000-0000-0000-0000-000000000002",
        user_id="00000000-0000-0000-0000-000000000003",
        platform="codex",
        session_id="session-3",
    )

    assert row["record_type"] == "invalid_json"
    assert row["payload"] is None
    assert row["raw_text"].startswith('{"broken"')
    assert row["parse_error"]


def test_record_ids_are_stable():
    importer = load_importer()
    kwargs = {
        "raw_line": b'{"type":"event_msg"}',
        "seq": 42,
        "transcript_id": "00000000-0000-0000-0000-000000000001",
        "import_id": "00000000-0000-0000-0000-000000000002",
        "user_id": "00000000-0000-0000-0000-000000000003",
        "platform": "codex",
        "session_id": "session-4",
    }
    assert importer.archive_record_row(**kwargs)["id"] == importer.archive_record_row(**kwargs)["id"]


def test_json_with_postgres_unsupported_nul_is_preserved_as_raw_text():
    importer = load_importer()
    row = importer.archive_record_row(
        raw_line=b'{"type":"event_msg","payload":{"message":"before\\u0000after"}}',
        seq=3,
        transcript_id="00000000-0000-0000-0000-000000000001",
        import_id="00000000-0000-0000-0000-000000000002",
        user_id="00000000-0000-0000-0000-000000000003",
        platform="codex",
        session_id="session-5",
    )

    assert row["payload"] is None
    assert "\\u0000" in row["raw_text"]
    assert "U+0000" in row["parse_error"]


def test_canonical_analysis_views_are_security_invoker_and_cover_core_shapes():
    migration = (
        ROOT
        / "plugins/codex-session-logging/supabase/migrations/20260711002854_canonical_session_analysis_views.sql"
    ).read_text(encoding="utf-8")

    for view in (
        "session_analysis_sessions",
        "session_analysis_messages",
        "session_analysis_tool_events",
        "session_analysis_usage",
        "session_analysis_models",
    ):
        assert f"create view public.{view}" in migration
        assert f"grant select on public.{view} to authenticated, service_role" in migration
    assert migration.count("with (security_invoker = true)") == 5
    assert "usage_rank = 1" in migration
    assert "not exists (" in migration
