import importlib.util
import json
from pathlib import Path
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
