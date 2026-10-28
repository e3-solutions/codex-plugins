"""COR-5027: open_session_passages calls link back to the search that returned the session.

Sesh search results now carry open_session_passages handles (result.open and result.source,
COR-4592/COR-4910). Before this, those handles made the delivered-digest list invalid, and opens
carried no linkage at all (412 opens since Oct 13, 0 linked). The open event now keeps the
search_request_id the handle carries, read vs "did it help" (kind), the offset, and a digest of
the session/revision handle that matches the search's delivered digests. Never the query,
passages or helped_reason. Same code in the Codex logger, the standalone Codex sesh plugin and
the Claude Code sesh plugin.
"""
from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
LOGGER = ROOT / "plugins/codex-session-logging/scripts/session_logging.py"
CODEX_SESH = ROOT / "plugins/sesh/scripts/session_logging.py"
CLAUDE_SCRIPTS = ROOT / "plugins/claude-sesh/scripts"

REQUEST_ID = "3f2a1b4c-5d6e-4f70-8a91-b2c3d4e5f607"
SOURCE_SESSION = "019a2b3c-1111-7000-8000-000000000001"
REVISION = "rev:" + "c" * 40
OTHER_SESSION = "019a2b3c-2222-7000-8000-000000000002"
OTHER_REVISION = "rev:" + "d" * 40
SECRET = "PRIVATE_PASSAGE_SENTINEL"
QUERY = "PRIVATE_QUERY_SENTINEL"
OPEN_TOOLS = [
    "mcp__cosmos__sesh__open_session_passages",
    "mcp__e3__sesh__open_session_passages",
    "mcp__codex_apps__cosmos__sesh__open_session_passages",
    "mcp__coreedge__sesh__open_session_passages",
    "mcp__e3_mcp__sesh__open_session_passages",
    "mcp__cf0ec861-5a4e-415b-b73e-6e017f49ace3__sesh__open_session_passages",
]


def load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(params=["logger", "codex_sesh"])
def hook(request, monkeypatch, tmp_path):
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex"))
    monkeypatch.setenv("CODEX_SESSION_LOG_AUTO_UPLOAD", "0")
    path = LOGGER if request.param == "logger" else CODEX_SESH
    return load(path, f"cor5027_{request.param}")


def handle_digest(session: str, revision: str) -> str:
    encoded = json.dumps({"revision_id": revision, "session_id": session}, sort_keys=True,
                         separators=(",", ":"), ensure_ascii=True, allow_nan=False)
    return hashlib.sha256(f"v1\nsesh__open_session_passages\n{encoded}".encode("ascii")).hexdigest()


def open_handle(session: str, revision: str, **extra) -> dict:
    return {"tool": "open_session_passages",
            "arguments": {"session_id": session, "revision_id": revision, "offset": 0,
                          "search_request_id": REQUEST_ID, **extra}}


def live_search_body() -> dict:
    """The shape Sesh returns today with the opener and feedback on (remember_output)."""
    fork = {"provider": "Sesh indexed passages", **open_handle(OTHER_SESSION, OTHER_REVISION, query=QUERY),
            "message_ids": []}
    return {"search_request_id": REQUEST_ID, "results": [{
        "source_session_id": SOURCE_SESSION, "revision_id": REVISION,
        "open": open_handle(SOURCE_SESSION, REVISION),
        "source": {"provider": "Sesh indexed passages", **open_handle(SOURCE_SESSION, REVISION),
                   "message_ids": ["m1"]},
        "evidence": [
            {"passage": SECRET, "source": {"provider": "Sesh indexed passages",
                                           **open_handle(SOURCE_SESSION, REVISION), "message_ids": []}},
            {"passage": SECRET, "source": fork},
            {"passage": SECRET, "source": {"provider": "Sesh indexed passages", "available": False,
                                           "message_ids": [], "note": "No revision-aware open handle."}},
        ],
    }]}


def envelope(body: dict) -> dict:
    return {"content": [{"type": "text", "text": json.dumps(body)}]}


def finished(hook, tool_name: str, tool_input: dict, response=None, **extra) -> dict:
    payload = {"tool_name": tool_name, "tool_input": tool_input,
               "tool_response": response if response is not None else
               {"content": [{"type": "text", "text": json.dumps({"found": True, "windows": [SECRET]})}]},
               **extra}
    event, metadata = hook.event_from_payload("PostToolUse", payload)
    assert event == "tool_call_finished"
    return metadata


def test_search_delivers_open_handle_digests_that_an_open_matches(hook):
    _, search = hook.event_from_payload("PostToolUse", {
        "tool_name": "mcp__cosmos__sesh__search_coding_sessions", "tool_input": {"query": QUERY},
        "tool_response": envelope(live_search_body())})
    assert search["sesh_request_id"] == REQUEST_ID
    # Unavailable evidence is skipped instead of voiding the list; the fork's query is not hashed in.
    assert search["sesh_delivered_source_handle_sha256_v1"] == [
        handle_digest(SOURCE_SESSION, REVISION), handle_digest(OTHER_SESSION, OTHER_REVISION)]

    opened = finished(hook, "mcp__cosmos__sesh__open_session_passages",
                      open_handle(SOURCE_SESSION, REVISION)["arguments"])
    assert opened["sesh_open_search_request_id"] == REQUEST_ID
    assert opened["sesh_opened_source_handle_sha256_v1"] in search["sesh_delivered_source_handle_sha256_v1"]
    for text in (SECRET, QUERY, SOURCE_SESSION, REVISION):
        assert text not in json.dumps(search) + json.dumps(opened)


@pytest.mark.parametrize("tool_name", OPEN_TOOLS)
def test_open_records_request_id_kind_offset_and_handle_digest(hook, tool_name):
    metadata = finished(hook, tool_name, {"session_id": SOURCE_SESSION, "revision_id": REVISION,
                                          "offset": 6, "search_request_id": REQUEST_ID, "query": QUERY})
    assert {key: value for key, value in metadata.items() if key.startswith("sesh_")} == {
        "sesh_open_kind": "open",
        "sesh_open_search_request_id": REQUEST_ID,
        "sesh_open_offset": 6,
        "sesh_opened_source_handle_sha256_v1": handle_digest(SOURCE_SESSION, REVISION),
        "sesh_source_open_witness_v1": "unknown",
    }
    assert QUERY not in json.dumps(metadata) and SECRET not in json.dumps(metadata)


def test_open_without_search_request_id_or_offset(hook):
    metadata = finished(hook, OPEN_TOOLS[0], {"session_id": SOURCE_SESSION, "revision_id": REVISION})
    assert "sesh_open_search_request_id" not in metadata
    assert metadata["sesh_open_offset"] == 0
    assert metadata["sesh_opened_source_handle_sha256_v1"] == handle_digest(SOURCE_SESSION, REVISION)


def test_feedback_call_is_not_an_open(hook):
    metadata = finished(hook, OPEN_TOOLS[0], {"session_id": SOURCE_SESSION, "search_request_id": REQUEST_ID,
                                              "helped": "partly", "helped_reason": SECRET})
    assert metadata["sesh_open_kind"] == "feedback"
    assert metadata["sesh_open_search_request_id"] == REQUEST_ID
    for key in ("sesh_open_offset", "sesh_opened_source_handle_sha256_v1", "sesh_source_open_witness_v1"):
        assert key not in metadata
    assert SECRET not in json.dumps(metadata)


def test_error_response_is_a_failed_open(hook):
    metadata = finished(hook, OPEN_TOOLS[0], {"session_id": SOURCE_SESSION, "revision_id": REVISION},
                        response={"isError": True, "content": [{"type": "text", "text": SECRET}]})
    assert metadata["sesh_source_open_witness_v1"] == "failed"


@pytest.mark.parametrize("tool_input", [
    {"session_id": SOURCE_SESSION, "revision_id": REVISION, "unexpected": 1},
    "not a dict",
    None,
])
def test_unexpected_input_records_nothing(hook, tool_input):
    metadata = finished(hook, OPEN_TOOLS[0], tool_input)
    assert not [key for key in metadata if key.startswith("sesh_")]


@pytest.mark.parametrize("tool_input", [
    {"session_id": SOURCE_SESSION, "revision_id": REVISION, "search_request_id": "not-a-uuid", "offset": 1001},
    {"session_id": SOURCE_SESSION, "revision_id": REVISION, "search_request_id": REQUEST_ID.upper(),
     "offset": True},
])
def test_bad_values_are_dropped_one_by_one(hook, tool_input):
    metadata = finished(hook, OPEN_TOOLS[0], tool_input)
    assert "sesh_open_search_request_id" not in metadata and "sesh_open_offset" not in metadata
    assert metadata["sesh_open_kind"] == "open"
    assert metadata["sesh_opened_source_handle_sha256_v1"] == handle_digest(SOURCE_SESSION, REVISION)


@pytest.mark.parametrize("revision", [None, "", " padded", "a\nb", "x" * 201])
def test_no_digest_without_a_clean_revision(hook, revision):
    arguments = {"session_id": SOURCE_SESSION, "offset": 0}
    if revision is not None:
        arguments["revision_id"] = revision
    metadata = finished(hook, OPEN_TOOLS[0], arguments)
    assert "sesh_opened_source_handle_sha256_v1" not in metadata


@pytest.mark.parametrize("tool_name", ["mcp__cosmos__sesh__open_session_passages_v2",
                                       "mcp__cosmos__forum__open_session_passages_x", "Bash"])
def test_other_tools_are_not_linked(hook, tool_name):
    metadata = finished(hook, tool_name, {"session_id": SOURCE_SESSION, "revision_id": REVISION})
    assert not [key for key in metadata if key.startswith("sesh_open")]


def test_claude_code_sesh_plugin_links_opens_the_same_way(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(CLAUDE_SCRIPTS))
    # Fresh copies; monkeypatch.setitem restores (or removes) both sys.modules entries afterwards,
    # so other test modules keep their own sesh_cc / sesh_linking.
    for name in ("sesh_cc", "sesh_linking"):
        monkeypatch.setitem(sys.modules, name, sys.modules.get(name))
    load(CLAUDE_SCRIPTS / "sesh_cc.py", "sesh_cc")
    linking = load(CLAUDE_SCRIPTS / "sesh_linking.py", "sesh_linking")
    # Claude Code hands MCP results to hooks as a string or a list of content blocks.
    search = linking.search_metadata({"tool_name": "mcp__cosmos__sesh__search_coding_sessions",
                                      "tool_response": json.dumps(live_search_body())})
    assert search["sesh_delivered_source_handle_sha256_v1"][0] == handle_digest(SOURCE_SESSION, REVISION)
    payload = {"hook_event_name": "PostToolUse", "tool_name": OPEN_TOOLS[-1],
               "tool_input": open_handle(SOURCE_SESSION, REVISION)["arguments"],
               "tool_response": [{"type": "text", "text": SECRET}]}
    metadata = linking.open_passages_metadata(payload, linking.tool_success(payload))
    assert metadata == {"sesh_open_kind": "open", "sesh_open_search_request_id": REQUEST_ID,
                        "sesh_open_offset": 0,
                        "sesh_opened_source_handle_sha256_v1": handle_digest(SOURCE_SESSION, REVISION),
                        "sesh_source_open_witness_v1": "unknown"}


def test_three_copies_share_the_linkage_code():
    def block(path: Path) -> str:
        text = path.read_text()
        start = text.index("def _canonical_uuid(value):")
        end = text.index("def _per_query_request_ids(body):")
        return text[start:end]

    assert block(LOGGER) == block(CODEX_SESH) == block(CLAUDE_SCRIPTS / "sesh_linking.py")
