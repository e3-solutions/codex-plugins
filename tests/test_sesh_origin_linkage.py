import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from test_codex_session_logging import load_session_logging, SESH_SEARCH_TOOL, SESH_REQUEST_ID


PARENT = "11111111-1111-4111-8111-111111111111"
CHILD = "22222222-2222-4222-8222-222222222222"


def fixture_payload(tmp_path, origin=CHILD, context=PARENT):
    path = tmp_path / f"rollout-2026-09-18T15-00-00-{origin}.jsonl"
    header = {"type": "session_meta", "payload": {"id": origin,
        "source": {"subagent": {"thread_spawn": {"parent_thread_id": context}}}}}
    path.write_text(json.dumps(header) + "\nPRIVATE BODY MUST NOT BE READ\n")
    return {"session_id": context, "transcript_path": str(path),
        "tool_name": SESH_SEARCH_TOOL,
        "tool_response": {"search_request_id": SESH_REQUEST_ID}}, path, header


@pytest.mark.parametrize("same", [False, True])
def test_verified_direct_or_child_origin(tmp_path, same):
    module = load_session_logging()
    payload, _, _ = fixture_payload(tmp_path, context=CHILD if same else PARENT)
    _, metadata = module.event_from_payload("PostToolUse", payload)
    assert metadata["sesh_origin_session_id"] == CHILD
    assert metadata["sesh_context_session_id"] == payload["session_id"]
    assert metadata["sesh_origin_basis"] == "client_transcript_header_v1"
    assert "PRIVATE" not in repr(metadata)


@pytest.mark.parametrize("failure", ["missing", "bad_json", "oversize", "wrong_parent", "wrong_id", "wrong_type", "symlink"])
def test_ambiguous_origin_keeps_request_but_omits_link(tmp_path, failure):
    module = load_session_logging()
    payload, path, header = fixture_payload(tmp_path)
    if failure == "missing":
        path.unlink()
    elif failure == "bad_json":
        path.write_text("{")
    elif failure == "oversize":
        path.write_text(" " * 524289)
    elif failure == "symlink":
        target = tmp_path / "target"
        path.rename(target)
        path.symlink_to(target)
    else:
        if failure == "wrong_parent":
            header["payload"]["source"] = "cli"
        elif failure == "wrong_id":
            header["payload"]["id"] = PARENT
        else:
            header["type"] = "response_item"
        path.write_text(json.dumps(header))
    _, metadata = module.event_from_payload("PostToolUse", payload)
    assert metadata["sesh_request_id"] == SESH_REQUEST_ID
    assert "sesh_origin_session_id" not in metadata


def test_no_origin_read_without_finished_search_receipt(tmp_path, monkeypatch):
    module = load_session_logging()
    payload, _, _ = fixture_payload(tmp_path)
    def forbidden(*args):
        raise AssertionError("must not read transcript")
    monkeypatch.setattr(module, "search_origin_metadata", forbidden)
    module.event_from_payload("PreToolUse", payload)
    payload["tool_name"] = "unrelated"
    module.event_from_payload("PostToolUse", payload)


def test_large_realistic_header_and_conflicting_parent(tmp_path):
    module = load_session_logging()
    payload, path, header = fixture_payload(tmp_path)
    header["payload"].update({"session_id": PARENT, "parent_thread_id": PARENT,
                              "instructions": "x" * 20000})
    path.write_text(json.dumps(header))
    assert module.search_origin_metadata(payload)["sesh_origin_session_id"] == CHILD
    header["payload"]["parent_thread_id"] = CHILD
    path.write_text(json.dumps(header))
    assert module.search_origin_metadata(payload) == {}
    header["payload"]["parent_thread_id"] = PARENT
    header["payload"]["session_id"] = CHILD
    path.write_text(json.dumps(header))
    assert module.search_origin_metadata(payload) == {}


def test_direct_parent_only(tmp_path):
    module = load_session_logging()
    payload, path, header = fixture_payload(tmp_path)
    header["payload"].pop("source")
    header["payload"]["parent_thread_id"] = PARENT
    path.write_text(json.dumps(header))
    assert module.search_origin_metadata(payload)["sesh_origin_session_id"] == CHILD


def test_capture_to_server_sanitizer_roundtrip(tmp_path, monkeypatch):
    deno = os.environ.get("SESH_TEST_DENO") or shutil.which("deno")
    if not deno:
        pytest.skip("Deno required for cross-runtime contract test")
    module = load_session_logging()
    payload, _, _ = fixture_payload(tmp_path)
    payload["tool_input"] = {"query": "PRIVATE QUESTION"}
    monkeypatch.setenv("CODEX_SESSION_LOG_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("CODEX_SESSION_LOG_AUTO_UPLOAD", "0")
    event_type, metadata = module.event_from_payload("PostToolUse", payload)
    record = module.capture_metadata_event(payload, hook_event="PostToolUse",
        event_type=event_type, event_metadata=metadata)
    event = json.loads((tmp_path / "state" / record["local_content_path"]).read_text())
    sanitizer = (Path(__file__).resolve().parents[1] / "plugins/codex-session-logging/"
                 "supabase/functions/codex-session-ingest/event_sanitizer.ts")
    script = ('import { sanitizeEventPayload } from ' + json.dumps(sanitizer.as_uri()) + ';'
              'const raw=await new Response(Deno.stdin.readable).text();'
              'const x=JSON.parse(raw); console.log(JSON.stringify(sanitizeEventPayload(x.record,x.event)));')
    result = subprocess.run([deno, "eval", script], input=json.dumps({"record": record, "event": event}),
        text=True, capture_output=True, check=True, timeout=20)
    clean = json.loads(result.stdout)
    assert clean["session_id"] == PARENT
    assert clean["metadata"]["sesh_origin_session_id"] == CHILD
    assert clean["metadata"]["sesh_context_session_id"] == PARENT
    assert clean["metadata"]["sesh_request_id"] == SESH_REQUEST_ID
    assert "PRIVATE" not in json.dumps(clean)
