from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = ROOT / "plugins/codex-session-logging/scripts/session_logging.py"
REQUEST_ID = "11111111-1111-4111-8111-111111111111"
REVISION = "a" * 64
MESSAGE_ID = "33333333-3333-4333-8333-333333333333"
SESSION_ID = "44444444-4444-4444-8444-444444444444"


@pytest.fixture
def hook(monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("CODEX_SESSION_LOG_AUTO_UPLOAD", "0")
    spec = importlib.util.spec_from_file_location("sesh_yield_hook", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def expected_digest(leaf: str, arguments: dict) -> str:
    encoded = json.dumps(arguments, sort_keys=True, separators=(",", ":"),
                         ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(f"v1\n{leaf}\n{encoded}".encode("ascii")).hexdigest()


def rollout_source(message_id: str = MESSAGE_ID) -> dict:
    return {"provider": "Sesh", "tool": "open_coding_session_source",
            "arguments": {"reference_id": f"{REVISION}:{message_id}"},
            "message_ids": [message_id]}


def cosmos_source() -> dict:
    return {"provider": "Cosmos Coding Sessions", "tool": "get_chat",
            "arguments": {"sessionId": SESSION_ID, "includeContent": True},
            "message_ids": [MESSAGE_ID]}


def search_body(*, sources=None) -> dict:
    sources = [rollout_source(), cosmos_source()] if sources is None else sources
    return {"search_request_id": REQUEST_ID, "results": [{
        "source": sources[0],
        "evidence": [{"source": source, "passage": "PRIVATE PASSAGE"}
                     for source in sources],
    }]}


def test_search_records_only_ordered_deduplicated_delivered_handle_digests(hook):
    body = search_body()
    _, metadata = hook.event_from_payload("PostToolUse", {
        "tool_name": "mcp__e3_cosmos__sesh__search_coding_sessions",
        "tool_response": {"structuredContent": body,
            "content": [{"type": "text", "text": json.dumps(body)}]},
    })
    assert metadata["sesh_request_id"] == REQUEST_ID
    assert metadata["sesh_delivered_source_handle_sha256_v1"] == [
        expected_digest("sesh__open_coding_session_source",
                        {"reference_id": f"{REVISION}:{MESSAGE_ID}"}),
        expected_digest("timetracker__get_chat",
                        {"sessionId": SESSION_ID, "includeContent": True}),
    ]
    serialized = json.dumps(metadata)
    for private in ("PRIVATE PASSAGE", REVISION, MESSAGE_ID, SESSION_ID):
        assert private not in serialized


@pytest.mark.parametrize("namespace", ["e3_cosmos", "e3", "cosmos", "cosmos_e3"])
@pytest.mark.parametrize("envelope", ["direct", "structured", "text"])
def test_valid_nonempty_digest_across_aliases_and_response_envelopes(
        hook, namespace, envelope):
    body = search_body()
    if envelope == "direct":
        response = body
    elif envelope == "structured":
        response = {"structuredContent": body}
    else:
        response = {"content": [{"type": "text", "text": json.dumps(body)}]}
    _, metadata = hook.event_from_payload("PostToolUse", {
        "tool_name": f"mcp__{namespace}__sesh__search_coding_sessions",
        "tool_response": response,
    })
    assert metadata["sesh_request_id"] == REQUEST_ID
    assert metadata["sesh_delivered_source_handle_sha256_v1"] == [
        expected_digest("sesh__open_coding_session_source",
                        {"reference_id": f"{REVISION}:{MESSAGE_ID}"}),
        expected_digest("timetracker__get_chat",
                        {"sessionId": SESSION_ID, "includeContent": True}),
    ]


def test_oversized_json_text_envelope_omits_all_sesh_metadata(hook):
    body = {**search_body(), "padding": "PRIVATE OVERSIZED " * 20000}
    raw = json.dumps(body)
    assert len(raw.encode("utf-8")) > hook.SESH_MAX_RESPONSE_BYTES
    _, metadata = hook.event_from_payload("PostToolUse", {
        "tool_name": "mcp__e3_cosmos__sesh__search_coding_sessions",
        "tool_response": {"content": [{"type": "text", "text": raw}]},
    })
    assert "sesh_request_id" not in metadata
    assert "sesh_delivered_source_handle_sha256_v1" not in metadata
    assert "PRIVATE OVERSIZED" not in json.dumps(metadata)


def test_empty_search_records_parsed_empty_delivery_set(hook):
    _, metadata = hook.event_from_payload("PostToolUse", {
        "tool_name": "mcp__cosmos__sesh__search_coding_sessions",
        "tool_response": {"search_request_id": REQUEST_ID, "results": []},
    })
    assert metadata["sesh_delivered_source_handle_sha256_v1"] == []


@pytest.mark.parametrize("body", [
    search_body(sources=[{"tool": "get_chat", "arguments": {
        "sessionId": SESSION_ID, "includeContent": False}}]),
    {"search_request_id": REQUEST_ID, "results": [{"evidence": [{"passage": "secret"}]}]},
    {"search_request_id": REQUEST_ID, "results": "not-a-list"},
])
def test_malformed_handles_omit_optional_digest_but_keep_request_id(hook, body):
    _, metadata = hook.event_from_payload("PostToolUse", {
        "tool_name": "mcp__e3__sesh__search_coding_sessions",
        "tool_response": body,
    })
    assert metadata["sesh_request_id"] == REQUEST_ID
    assert "sesh_delivered_source_handle_sha256_v1" not in metadata
    assert "secret" not in json.dumps(metadata)


def test_conflicting_delivery_envelopes_omit_digest(hook):
    other = search_body(sources=[rollout_source(
        "55555555-5555-4555-8555-555555555555")])
    _, metadata = hook.event_from_payload("PostToolUse", {
        "tool_name": "mcp__cosmos_e3__sesh__search_coding_sessions",
        "tool_response": {**search_body(), "structuredContent": other},
    })
    assert metadata["sesh_request_id"] == REQUEST_ID
    assert "sesh_delivered_source_handle_sha256_v1" not in metadata


def test_more_than_seventy_five_unique_handles_omits_digest(hook):
    additional = [rollout_source(
        f"{index:08x}-0000-0000-0000-{index:012x}") for index in range(1, 76)]
    source = {**rollout_source(), "additional_sources": additional}
    _, metadata = hook.event_from_payload("PostToolUse", {
        "tool_name": "mcp__e3_cosmos__sesh__search_coding_sessions",
        "tool_response": search_body(sources=[source]),
    })
    assert metadata["sesh_request_id"] == REQUEST_ID
    assert "sesh_delivered_source_handle_sha256_v1" not in metadata


def test_more_than_eighty_duplicate_handle_occurrences_omits_digest(hook):
    source = {**rollout_source(),
              "additional_sources": [rollout_source() for _ in range(80)]}
    _, metadata = hook.event_from_payload("PostToolUse", {
        "tool_name": "mcp__e3_cosmos__sesh__search_coding_sessions",
        "tool_response": search_body(sources=[source]),
    })
    assert metadata["sesh_request_id"] == REQUEST_ID
    assert "sesh_delivered_source_handle_sha256_v1" not in metadata


def test_maximum_valid_envelope_retains_seventy_five_unique_digests(hook):
    results = []
    expected = []
    for result_index in range(5):
        sources = []
        for evidence_index in range(15):
            sequence = result_index * 15 + evidence_index + 1
            message_id = f"{sequence:08x}-0000-4000-8000-{sequence:012x}"
            source = rollout_source(message_id)
            sources.append(source)
            expected.append(expected_digest(
                "sesh__open_coding_session_source",
                {"reference_id": f"{REVISION}:{message_id}"}))
        results.append({"source": sources[0],
                        "evidence": [{"source": source} for source in sources]})
    _, metadata = hook.event_from_payload("PostToolUse", {
        "tool_name": "mcp__e3_cosmos__sesh__search_coding_sessions",
        "tool_response": {"search_request_id": REQUEST_ID, "results": results},
    })
    assert metadata["sesh_delivered_source_handle_sha256_v1"] == expected


def test_deeply_nested_source_handles_omit_digest(hook):
    source = rollout_source()
    for _ in range(6):
        source = {**rollout_source(), "additional_sources": [source]}
    _, metadata = hook.event_from_payload("PostToolUse", {
        "tool_name": "mcp__e3_cosmos__sesh__search_coding_sessions",
        "tool_response": search_body(sources=[source]),
    })
    assert metadata["sesh_request_id"] == REQUEST_ID
    assert "sesh_delivered_source_handle_sha256_v1" not in metadata


@pytest.mark.parametrize("namespace", ["e3_cosmos", "e3", "cosmos", "cosmos_e3"])
def test_exact_sesh_source_open_records_digest_and_verified_witness(hook, namespace):
    reference_id = f"{REVISION}:{MESSAGE_ID}"
    _, metadata = hook.event_from_payload("PostToolUse", {
        "tool_name": f"mcp__{namespace}__sesh__open_coding_session_source",
        "tool_input": {"reference_id": reference_id},
        "tool_response": {"reference_id": reference_id, "message_id": MESSAGE_ID,
                          "verification": "exact_source_bytes",
                          "text": "PRIVATE SOURCE BODY"},
    })
    assert metadata["sesh_opened_source_handle_sha256_v1"] == expected_digest(
        "sesh__open_coding_session_source", {"reference_id": reference_id})
    assert metadata["sesh_source_open_witness_v1"] == "verified"
    assert "PRIVATE SOURCE BODY" not in json.dumps(metadata)
    assert reference_id not in json.dumps(metadata)


def test_cosmos_source_open_is_unknown_without_strict_response_witness(hook):
    _, metadata = hook.event_from_payload("PostToolUse", {
        "tool_name": "mcp__e3_cosmos__timetracker__get_chat",
        "tool_input": {"sessionId": SESSION_ID, "includeContent": True},
        "success": True,
        "tool_response": {"isError": False, "content": [
            {"type": "text", "text": "PRIVATE SOURCE BODY"}]},
    })
    assert metadata["sesh_opened_source_handle_sha256_v1"] == expected_digest(
        "timetracker__get_chat", {"sessionId": SESSION_ID, "includeContent": True})
    assert metadata["sesh_source_open_witness_v1"] == "unknown"
    assert "PRIVATE SOURCE BODY" not in json.dumps(metadata)


def test_explicit_source_open_failure_dominates_valid_success_witness(hook):
    reference_id = f"{REVISION}:{MESSAGE_ID}"
    _, metadata = hook.event_from_payload("PostToolUse", {
        "tool_name": "mcp__e3__sesh__open_coding_session_source",
        "tool_input": {"reference_id": reference_id},
        "success": False,
        "tool_response": {"reference_id": reference_id, "message_id": MESSAGE_ID,
                          "verification": "exact_source_bytes"},
    })
    assert metadata["sesh_source_open_witness_v1"] == "failed"
    assert metadata["success"] is False


@pytest.mark.parametrize("response", [
    {"reference_id": f"{REVISION}:{MESSAGE_ID}",
     "message_id": "55555555-5555-4555-8555-555555555555",
     "verification": "exact_source_bytes"},
    {"reference_id": f"{REVISION}:{MESSAGE_ID}", "message_id": MESSAGE_ID},
    {"isError": False},
])
def test_missing_or_mismatched_sesh_source_witness_is_unknown(hook, response):
    reference_id = f"{REVISION}:{MESSAGE_ID}"
    _, metadata = hook.event_from_payload("PostToolUse", {
        "tool_name": "mcp__cosmos__sesh__open_coding_session_source",
        "tool_input": {"reference_id": reference_id},
        "tool_response": response,
    })
    assert metadata["sesh_source_open_witness_v1"] == "unknown"


@pytest.mark.parametrize("tool_name,tool_input", [
    ("mcp__other__sesh__open_coding_session_source",
     {"reference_id": f"{REVISION}:{MESSAGE_ID}"}),
    ("mcp__e3__sesh__open_coding_session_source", {"reference_id": "private"}),
    ("mcp__e3__timetracker__get_chat",
     {"sessionId": SESSION_ID, "includeContent": False}),
])
def test_unsupported_or_malformed_open_has_no_yield_metadata(hook, tool_name, tool_input):
    _, metadata = hook.event_from_payload("PostToolUse", {
        "tool_name": tool_name, "tool_input": tool_input,
        "tool_response": {"isError": False},
    })
    assert "sesh_opened_source_handle_sha256_v1" not in metadata
    assert "sesh_source_open_witness_v1" not in metadata
