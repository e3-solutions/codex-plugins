import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest


@pytest.fixture
def hook(monkeypatch, tmp_path):
    monkeypatch.setenv('CODEX_HOME', str(tmp_path / 'codex'))
    monkeypatch.setenv('CODEX_SESSION_LOG_AUTO_UPLOAD', '0')
    path = Path(__file__).parents[1] / 'plugins/codex-session-logging/scripts/session_logging.py'
    spec = importlib.util.spec_from_file_location('source_outcome_hook', path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize('namespace', ['e3_cosmos', 'e3', 'cosmos', 'cosmos_e3'])
@pytest.mark.parametrize('tool', ['timetracker__get_chat', 'sesh__open_coding_session_source'])
def test_explicit_mcp_error_overrides_outer_success(hook, namespace, tool):
    payload = {'tool_name': f'mcp__{namespace}__{tool}', 'success': True,
               'tool_response': {'isError': True, 'content': [
                   {'type': 'text', 'text': 'PRIVATE_SOURCE_SENTINEL'}]}}
    event, metadata = hook.event_from_payload('PostToolUse', payload)
    assert event == 'tool_call_finished'
    assert metadata['success'] is False
    assert 'PRIVATE_SOURCE_SENTINEL' not in json.dumps(metadata)


@pytest.mark.parametrize('response', [None, {}, {'isError': False},
    {'isError': False, 'content': []}, {'isError': 'true'},
    {'isError': 1}, {'content': [{'type': 'text', 'text': '{"isError":true}'}]}])
def test_no_source_success_without_witness(hook, response):
    payload = {'tool_name': 'mcp__e3_cosmos__timetracker__get_chat',
               'success': True, 'tool_response': response}
    assert hook.tool_success(payload) is None


def test_reported_failure_preserved(hook):
    assert hook.tool_success({'tool_name': 'mcp__e3_cosmos__timetracker__get_chat',
        'success': False, 'tool_response': {'isError': False}}) is False


@pytest.mark.parametrize('failure', [{'ok': False}, {'succeeded': False},
    {'status': 'failed'}, {'status': 'success', 'result': 'error'}])
def test_conflicting_failure_dominates_for_source_opens(hook, failure):
    assert hook.tool_success({'tool_name': 'mcp__e3_cosmos__timetracker__get_chat',
        'success': True, **failure, 'tool_response': {'isError': False}}) is False


def test_unknown_is_omitted_from_finished_metadata(hook):
    _, metadata = hook.event_from_payload('PostToolUse', {
        'tool_name': 'mcp__e3_cosmos__timetracker__get_chat',
        'success': True, 'tool_response': {'isError': False, 'content': []}})
    assert 'success' not in metadata


@pytest.mark.parametrize('name', ['Bash', 'mcp__e3_cosmos__timetracker__get_chat_extra',
    'untrusted__sesh__open_coding_session_source'])
def test_other_tools_keep_existing_semantics(hook, name):
    assert hook.tool_success({'tool_name': name, 'success': True,
        'tool_response': {'isError': True}}) is True


@pytest.mark.parametrize('tool', ['timetracker__get_chat', 'sesh__open_coding_session_source'])
@pytest.mark.parametrize('response,expected', [
    ({'isError': True}, False), ({'isError': False}, None), ({'isError': 'true'}, None)])
def test_capture_to_ingestion_preserves_outcome_without_content(hook, tmp_path, monkeypatch, tool, response, expected):
    deno = os.environ.get('SESH_TEST_DENO') or shutil.which('deno')
    if not deno:
        pytest.skip('Deno required for actual ingestion sanitizer roundtrip')
    monkeypatch.setenv('CODEX_SESSION_LOG_STATE_DIR', str(tmp_path / 'state'))
    drains = []
    monkeypatch.setattr(hook, 'spawn_drain', lambda: drains.append(True))
    payload = {'session_id': '11111111-1111-4111-8111-111111111111',
        'tool_name': f'mcp__e3_cosmos__{tool}', 'success': True,
        'tool_input': {'reference_id': 'PRIVATE_HANDLE'},
        'tool_response': {**response, 'content': [{'type': 'text', 'text': 'PRIVATE_BODY'}]}}
    event_type, metadata = hook.event_from_payload('PostToolUse', payload)
    record = hook.capture_metadata_event(payload, hook_event='PostToolUse',
        event_type=event_type, event_metadata=metadata)
    event = json.loads((tmp_path / 'state' / record['local_content_path']).read_text())
    sanitizer = (Path(__file__).parents[1] / 'plugins/codex-session-logging/'
        'supabase/functions/codex-session-ingest/event_sanitizer.ts')
    script = ('import { sanitizeEventPayload } from ' + json.dumps(sanitizer.as_uri()) + ';'
        'const x=JSON.parse(await new Response(Deno.stdin.readable).text());'
        'console.log(JSON.stringify(sanitizeEventPayload(x.record,x.event)));')
    result = subprocess.run([deno, 'eval', script], text=True,
        input=json.dumps({'record': record, 'event': event}),
        capture_output=True, check=True, timeout=20)
    clean = json.loads(result.stdout)
    if expected is None:
        assert 'success' not in clean['metadata']
    else:
        assert clean['metadata']['success'] is expected
    assert 'PRIVATE_' not in json.dumps(clean)
    assert drains == []
