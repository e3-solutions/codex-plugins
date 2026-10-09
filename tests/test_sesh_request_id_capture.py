"""COR-4646: the logger links every Codex Sesh search, whatever its size, namespace or question count."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "plugins/codex-session-logging/scripts/session_logging.py"
FIRST = "11111111-1111-4111-8111-111111111111"
SECOND = "22222222-2222-4222-8222-222222222222"
THIRD = "33333333-3333-4333-9333-333333333333"
QUOTED = "44444444-4444-4444-8444-444444444444"
REVISION = "a" * 64


@pytest.fixture
def hook(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("CODEX_SESSION_LOG_AUTO_UPLOAD", "0")
    spec = importlib.util.spec_from_file_location("sesh_request_id_hook", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def result(index: int, passages: int, passage_bytes: int) -> dict:
    source = {"provider": "Sesh", "tool": "open_coding_session_source",
              "arguments": {"reference_id": f"{REVISION}:{index:08x}-0000-4000-8000-{index:012x}"}}
    return {"family_id": f"family-{index}", "source": source,
            "evidence": [{"source": source, "passage": "PRIVATE PASSAGE " + "x" * passage_bytes}
                         for _ in range(passages)]}


def live_shaped_answer(passage_bytes: int = 5000) -> dict:
    # Shape of a live uncompacted answer: 5 results, ~11 passages each, id appended last.
    return {"retrieval_status": "candidates_found",
            "results": [result(i, 11, passage_bytes) for i in range(1, 6)],
            "search_request_id": FIRST}


def mcp_envelope(body: dict, *, structured: bool = True) -> dict:
    envelope = {"content": [{"type": "text", "text": json.dumps(body)}], "isError": False}
    if structured:
        envelope["structuredContent"] = body
    return envelope


def finish(hook, tool_name: str, response: dict) -> dict:
    _, metadata = hook.event_from_payload("PostToolUse", {"tool_name": tool_name, "tool_response": response})
    return metadata


@pytest.mark.parametrize("structured", [True, False])
def test_live_sized_uncompacted_answer_is_linked(hook, structured):
    body = live_shaped_answer()
    assert len(json.dumps(body).encode()) > 262144  # the size that lost ids from Oct 5
    metadata = finish(hook, "mcp__cosmos__sesh__search_coding_sessions", mcp_envelope(body, structured=structured))
    assert metadata["sesh_request_id"] == FIRST
    assert metadata["sesh_request_ids"] == [FIRST]
    assert len(metadata["sesh_delivered_source_handle_sha256_v1"]) == 5
    assert "PRIVATE" not in json.dumps(metadata)


@pytest.mark.parametrize("tool_name", [
    "mcp__codex_apps__cosmos__sesh__search_coding_sessions",
    "mcp__e3_mcp__sesh__search_coding_sessions",
    "mcp__e3_mcp__search_coding_sessions",
    "mcp__some-new_ns__sesh__search_coding_sessions",
    "search_coding_sessions",
    "sesh__search_coding_sessions",
])
def test_any_sesh_search_namespace_is_linked(hook, tool_name):
    metadata = finish(hook, tool_name, mcp_envelope({"search_request_id": FIRST, "results": []}))
    assert metadata["sesh_request_id"] == FIRST


@pytest.mark.parametrize("tool_name", [
    "mcp__e3__sesh__search_coding_sessions_v2",
    "mcp__e3__sesh__open_coding_session_source",
    "other_search_coding_sessions",
    "mcp__e3__sesh__search_coding_sessions ",
    "mcp__e3 x__sesh__search_coding_sessions",
    "",
])
def test_other_tools_are_not_linked(hook, tool_name):
    metadata = finish(hook, tool_name, mcp_envelope({"search_request_id": FIRST}))
    assert "sesh_request_id" not in metadata
    assert "sesh_request_ids" not in metadata


def multi_answer() -> dict:
    # sesh_search/multiquery.fuse: per-question ids, top-level id of the first answered question.
    return {"results": [], "per_query": [
        {"query": "PRIVATE Q1", "status": "candidates_found", "search_request_id": FIRST},
        {"query": "PRIVATE Q2", "status": "error", "error": "search failed"},
        {"query": "PRIVATE Q3", "status": "candidates_found", "search_request_id": SECOND},
        {"query": "PRIVATE Q4", "status": "candidates_found", "search_request_id": THIRD},
    ], "search_request_id": FIRST}


def test_multi_question_call_records_every_request_id(hook):
    metadata = finish(hook, "mcp__e3__sesh__search_coding_sessions", mcp_envelope(multi_answer()))
    assert metadata["sesh_request_id"] == FIRST
    assert metadata["sesh_request_ids"] == [FIRST, SECOND, THIRD]
    assert "PRIVATE" not in json.dumps(metadata)


def test_multi_question_without_top_level_id_uses_first_question(hook):
    body = multi_answer()
    del body["search_request_id"]
    metadata = finish(hook, "mcp__e3__sesh__search_coding_sessions", {"structuredContent": body})
    assert metadata["sesh_request_id"] == FIRST
    assert metadata["sesh_request_ids"] == [FIRST, SECOND, THIRD]


def test_noncanonical_per_query_ids_are_skipped(hook):
    body = multi_answer()
    body["per_query"][2]["search_request_id"] = "not-a-uuid"
    metadata = finish(hook, "mcp__e3__sesh__search_coding_sessions", {"structuredContent": body})
    assert metadata["sesh_request_ids"] == [FIRST, THIRD]


def test_text_beyond_parse_cap_is_scanned_for_structural_ids_only(hook, monkeypatch):
    monkeypatch.setattr(hook, "SESH_MAX_RESPONSE_BYTES", 1024)
    body = multi_answer()
    # A passage quoting an id is escaped in JSON text and must never be read.
    body["results"] = [{"evidence": [{"passage": json.dumps({"search_request_id": QUOTED}) + "y" * 4000}]}]
    raw = json.dumps(body)
    assert len(raw) > 1024
    metadata = finish(hook, "mcp__codex_apps__cosmos__sesh__search_coding_sessions",
                      {"content": [{"type": "text", "text": raw}]})
    assert metadata["sesh_request_id"] == FIRST
    assert metadata["sesh_request_ids"] == [FIRST, SECOND, THIRD]
    assert QUOTED not in json.dumps(metadata)
    assert "sesh_delivered_source_handle_sha256_v1" not in metadata


def test_scanned_text_agrees_with_structured_content(hook, monkeypatch):
    monkeypatch.setattr(hook, "SESH_MAX_RESPONSE_BYTES", 64)
    body = live_shaped_answer(passage_bytes=10)
    metadata = finish(hook, "mcp__e3__sesh__search_coding_sessions", mcp_envelope(body))
    assert metadata["sesh_request_id"] == FIRST
    conflict = {"structuredContent": {"search_request_id": SECOND},
                "content": [{"type": "text", "text": json.dumps(body)}]}
    assert "sesh_request_id" not in finish(hook, "mcp__e3__sesh__search_coding_sessions", conflict)


@pytest.mark.parametrize("text", [
    "[" + json.dumps({"search_request_id": FIRST}) + "]",
    '{"search_request_id": "AAAAAAAA-1111-4111-8111-11111111111B", "x": "' + "z" * 2000 + '"}',
    '{"x": "' + "z" * 2000 + '"}',
])
def test_scan_rejects_non_objects_noncanonical_or_missing_ids(hook, monkeypatch, text):
    monkeypatch.setattr(hook, "SESH_MAX_RESPONSE_BYTES", 64)
    metadata = finish(hook, "mcp__e3__sesh__search_coding_sessions",
                      {"content": [{"type": "text", "text": text}]})
    assert "sesh_request_id" not in metadata


def test_scan_has_an_upper_bound(hook, monkeypatch):
    monkeypatch.setattr(hook, "SESH_MAX_RESPONSE_BYTES", 64)
    monkeypatch.setattr(hook, "SESH_MAX_SCAN_BYTES", 128)
    raw = json.dumps({"pad": "z" * 500, "search_request_id": FIRST})
    metadata = finish(hook, "mcp__e3__sesh__search_coding_sessions",
                      {"content": [{"type": "text", "text": raw}]})
    assert "sesh_request_id" not in metadata
