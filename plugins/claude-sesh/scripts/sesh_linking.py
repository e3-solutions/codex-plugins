"""Sesh search linking for Claude Code: metadata-only events to the existing session ingest.

For Sesh tools only, this queues the events the Codex `sesh` plugin sends, in the same shape,
to the same `codex-session-ingest` function the Claude Code logger (claude-session-logging)
already uses, so `codex_session_events` readouts work for Claude Code sessions too:

- `environment_snapshot` at SessionStart, naming plugin `sesh`, its version and marketplace;
- `tool_call_started` / `tool_call_finished` / `tool_call_failed` for Sesh tools, with the
  Sesh request ids of a search (the search-to-session link), the arm and the timing;
- `sesh_plan_cue_shown` / `sesh_plan_cue_withheld` when the plan cue fires (topic hash only).

Never sent: prompts, assistant text, queries, passages, tool input or output, file paths of
written files, or any other tool. Same gates as claude-session-logging: e3-solutions
repositories only, and its upload opt-out CLAUDE_SESSION_LOG_AUTO_UPLOAD=0. E3_SESH_LINKING=0
turns all of it off. The search-receipt block below is copied from plugins/sesh (tests pin it).
"""
from __future__ import annotations

import contextlib
import fcntl
import getpass
import hashlib
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from sesh_cc import PLUGIN_NAME, PLUGIN_VERSION, _falsy, _prune_old, arm_metadata, digest, state_dir

JsonDict = dict[str, Any]

LINKING_ENV = "E3_SESH_LINKING"
# Shared with claude-session-logging, so one setting covers both.
INGEST_URL_ENV = "CLAUDE_SESSION_LOG_INGEST_URL"
INGEST_TOKEN_ENV = "CLAUDE_SESSION_LOG_INGEST_TOKEN"
AUTO_UPLOAD_ENV = "CLAUDE_SESSION_LOG_AUTO_UPLOAD"
DEFAULT_SUPABASE_URL = "https://pmdfllwuctzkdjiehezq.supabase.co"
DEFAULT_INGEST_URL = f"{DEFAULT_SUPABASE_URL}/functions/v1/codex-session-ingest"
DEFAULT_BUCKET = "codex-sessions"
ALLOWED_GITHUB_ORG = "e3-solutions"
PLATFORM = "claude-code"
AGENT = "claude"
PERMANENT_HTTP_STATUSES = {400, 403, 413, 415, 422}
# The ingest stores each event at <session>/events/<seq>-<event_type>.json. claude-session-logging
# numbers its events 1, 2, 3, ... in the same session, so this plugin counts from here and the two
# never write the same storage object (seq is an int4 column; the ingest's own synthetic events
# use negative numbers).
SEQ_OFFSET = 1_500_000_000
SKILL_NAME = "search-coding-sessions"
MAX_EVENT_AGE_SECONDS = 30 * 60


def linking_enabled(env=None) -> bool:
    env = os.environ if env is None else env
    return not _falsy(env.get(LINKING_ENV))


def read_stdin_json() -> JsonDict:
    raw = sys.stdin.read()
    if not raw.strip():
        return {}
    try:
        loaded = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    return loaded if isinstance(loaded, dict) else {}


# --- search receipt (copied verbatim from plugins/sesh/scripts/session_logging.py) ---------------

# or the bare tool name. Nothing before or after the name.
SESH_SEARCH_TOOL_PATTERN = re.compile(r'(?:[A-Za-z0-9_-]+__)?(?:sesh__)?search_coding_sessions')
# Full JSON parse of a text envelope up to this size. Uncompacted answers with many evidence
# passages pass 256 KB (Oct 5, 2026), and the request id sits at the end of the text.
SESH_MAX_RESPONSE_BYTES = 16 * 1024 * 1024
# Larger texts are not parsed; only structural (unescaped) search_request_id keys are read.
SESH_MAX_SCAN_BYTES = 64 * 1024 * 1024
SESH_MAX_REQUEST_IDS = 32
SESH_REQUEST_ID_KEY_PATTERN = re.compile(
    r'(?<!\\)"search_request_id"\s*:\s*"([^"\\]{0,64})"'
)
SESH_MAX_DELIVERED_SOURCE_HANDLES = 75
# 5 results x (source + open handle + 15 evidence) = 85, plus nested additional sources.
SESH_MAX_SOURCE_HANDLE_OCCURRENCES = 100
SESH_UUID_PATTERN = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}'
)
SESH_ANY_UUID_PATTERN = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
)
SESH_ROLLOUT_REFERENCE_PATTERN = re.compile(
    r'[0-9a-f]{64}:[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}'
)


def _sesh_unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError('ambiguous JSON object')
        value[key] = item
    return value


def _sesh_response_bodies(response):
    """Return only bounded, unambiguous response objects; never tool input."""
    if not isinstance(response, dict):
        return None
    bodies = []
    if 'search_request_id' in response or 'results' in response:
        bodies.append(response)
    structured = response.get('structuredContent')
    if 'structuredContent' in response and not isinstance(structured, dict):
        return None
    if isinstance(structured, dict):
        bodies.append(structured)
    content = response.get('content')
    if 'content' in response and (not isinstance(content, list) or len(content) != 1):
        return None
    if isinstance(content, list) and len(content) == 1:
        item = content[0]
        if not isinstance(item, dict) or item.get('type') != 'text':
            return None
        raw = item.get('text')
        if not isinstance(raw, str):
            return None
        if len(raw) > SESH_MAX_RESPONSE_BYTES:
            # Too big to parse: keep only the request ids, never the content.
            scanned = _sesh_scanned_request_body(raw)
            if scanned is None:
                return None
            bodies.append(scanned)
            return bodies
        try:
            decoded = json.loads(raw, object_pairs_hook=_sesh_unique_object)
        except (ValueError, UnicodeError, RecursionError):
            return None
        if not isinstance(decoded, dict):
            return None
        bodies.append(decoded)
    return bodies


def _sesh_scanned_request_body(raw):
    """Request ids of an oversized JSON text envelope, read without parsing it.

    Only keys whose opening quote is not escaped are structural, so ids quoted inside
    passages (always escaped in JSON text) are never read. The first id is the answer's own
    search_request_id when it is the last key, as Sesh writes it; per-question ids precede it.
    """
    if len(raw) > SESH_MAX_SCAN_BYTES or not raw.lstrip().startswith('{'):
        return None
    found = SESH_REQUEST_ID_KEY_PATTERN.findall(raw)
    if not found or len(found) > SESH_MAX_REQUEST_IDS + 1:
        return None
    if any(SESH_UUID_PATTERN.fullmatch(value) is None for value in found):
        return None
    # Sesh appends the top-level id last; earlier ones are per_query entries.
    return {'search_request_id': found[-1],
            'per_query': [{'search_request_id': value} for value in found[:-1]]}


def _canonical_uuid(value):
    if not isinstance(value, str) or SESH_ANY_UUID_PATTERN.fullmatch(value) is None:
        return False
    try:
        return str(uuid.UUID(value)) == value and uuid.UUID(value).int != 0
    except (ValueError, AttributeError):
        return False


def _canonical_source_handle(tool, arguments):
    """Validate the two exact displayed source-handle shapes."""
    if tool == 'open_coding_session_source':
        if not isinstance(arguments, dict) or set(arguments) != {'reference_id'}:
            return None
        reference_id = arguments.get('reference_id')
        if (not isinstance(reference_id, str) or
                SESH_ROLLOUT_REFERENCE_PATTERN.fullmatch(reference_id) is None):
            return None
        message_id = reference_id.rsplit(':', 1)[-1]
        if not _canonical_uuid(message_id):
            return None
        leaf = 'sesh__open_coding_session_source'
    elif tool == 'get_chat':
        if (not isinstance(arguments, dict) or
                set(arguments) != {'sessionId', 'includeContent'} or
                arguments.get('includeContent') is not True or
                not _canonical_uuid(arguments.get('sessionId'))):
            return None
        leaf = 'timetracker__get_chat'
    elif tool == 'open_session_passages':
        # COR-5027: the reader search results point at (COR-4910). The handle is the session and
        # revision only; offset, query, search_request_id and feedback never enter the digest.
        if not isinstance(arguments, dict) or not set(arguments) <= SESH_OPEN_PASSAGES_ARGUMENTS:
            return None
        session_id, revision_id = arguments.get('session_id'), arguments.get('revision_id')
        if not _bounded_handle_id(session_id, 256) or not _bounded_handle_id(revision_id, 200):
            return None
        arguments = {'session_id': session_id, 'revision_id': revision_id}
        leaf = 'sesh__open_session_passages'
    else:
        return None
    encoded = json.dumps(arguments, sort_keys=True, separators=(',', ':'),
                         ensure_ascii=True, allow_nan=False)
    return leaf, encoded


SESH_OPEN_PASSAGES_ARGUMENTS = frozenset({'session_id', 'revision_id', 'offset', 'query',
                                          'search_request_id', 'helped', 'helped_reason'})
SESH_OPEN_PASSAGES_TOOL_PATTERN = re.compile(r'(?:[A-Za-z0-9_-]+__)?(?:sesh__)?open_session_passages')
SESH_MAX_OPEN_OFFSET = 1000


def _bounded_handle_id(value, maximum):
    return (isinstance(value, str) and 0 < len(value) <= maximum and value.strip() == value and
            not any(ord(char) < 32 or ord(char) == 127 for char in value))


def _source_handle_digest(tool, arguments):
    canonical = _canonical_source_handle(tool, arguments)
    if canonical is None:
        return None
    leaf, encoded = canonical
    return hashlib.sha256(f'v1\n{leaf}\n{encoded}'.encode('ascii')).hexdigest()


def _delivered_source_handle_digests(body):
    if 'results' not in body:
        return None
    results = body.get('results')
    if not isinstance(results, list) or len(results) > 5:
        raise ValueError('invalid Sesh result count')
    digests = []
    seen = set()
    visited = 0

    def add_source(source, depth=0):
        nonlocal visited
        visited += 1
        if visited > SESH_MAX_SOURCE_HANDLE_OCCURRENCES or depth > 4:
            raise ValueError('too many or too deeply nested Sesh source handles')
        if not isinstance(source, dict):
            raise ValueError('invalid Sesh source handle')
        if source.get('available') is False and 'tool' not in source:
            return  # Sesh says this evidence has no open handle; nothing was delivered.
        digest = _source_handle_digest(source.get('tool'), source.get('arguments'))
        if digest is None:
            raise ValueError('invalid Sesh source handle')
        if digest not in seen:
            seen.add(digest)
            digests.append(digest)
            if len(digests) > SESH_MAX_DELIVERED_SOURCE_HANDLES:
                raise ValueError('too many unique Sesh source handles')
        additional = source.get('additional_sources', [])
        if not isinstance(additional, list):
            raise ValueError('invalid additional Sesh sources')
        for item in additional:
            add_source(item, depth + 1)

    for result in results:
        if not isinstance(result, dict):
            raise ValueError('invalid Sesh result')
        if 'source' not in result:
            raise ValueError('missing Sesh result source')
        add_source(result['source'])
        if 'open' in result:
            add_source(result['open'])
        evidence = result.get('evidence', [])
        if not isinstance(evidence, list) or len(evidence) > 15:
            raise ValueError('invalid Sesh evidence count')
        for item in evidence:
            if not isinstance(item, dict) or 'source' not in item:
                raise ValueError('invalid Sesh evidence source')
            add_source(item['source'])
    return digests


def is_sesh_search_tool(name):
    return isinstance(name, str) and SESH_SEARCH_TOOL_PATTERN.fullmatch(name) is not None


def is_sesh_open_passages_tool(name):
    return (isinstance(name, str) and len(name) <= 256 and
            SESH_OPEN_PASSAGES_TOOL_PATTERN.fullmatch(name) is not None)


def open_passages_metadata(payload, success):
    """COR-5027: link an open_session_passages call to the search that returned the session.

    Keeps the search_request_id the open handle carries, whether the call read passages ("open")
    or only answered "did it help" ("feedback"), the page offset, and for reads the digest of the
    opened session/revision handle, matching sesh_delivered_source_handle_sha256_v1 on the search.
    Never the query, passages, helped_reason or any other tool input or output.
    """
    try:
        if not isinstance(payload, dict) or not is_sesh_open_passages_tool(payload.get('tool_name')):
            return {}
        arguments = payload.get('tool_input')
        if not isinstance(arguments, dict) or not set(arguments) <= SESH_OPEN_PASSAGES_ARGUMENTS:
            return {}
        kind = 'feedback' if 'helped' in arguments else 'open'
        metadata = {'sesh_open_kind': kind}
        request_id = arguments.get('search_request_id')
        if isinstance(request_id, str) and SESH_UUID_PATTERN.fullmatch(request_id) is not None:
            metadata['sesh_open_search_request_id'] = request_id
        if kind == 'open':
            offset = arguments.get('offset')
            offset = 0 if offset is None else offset
            if (isinstance(offset, int) and not isinstance(offset, bool) and
                    0 <= offset <= SESH_MAX_OPEN_OFFSET):
                metadata['sesh_open_offset'] = offset
            digest = _source_handle_digest('open_session_passages', arguments)
            if digest is not None:
                metadata['sesh_opened_source_handle_sha256_v1'] = digest
                # Passages are ranked excerpts, never exact source bytes: no "verified" witness.
                response = payload.get('tool_response')
                failed = success is False or (isinstance(response, dict) and response.get('isError') is True)
                metadata['sesh_source_open_witness_v1'] = 'failed' if failed else 'unknown'
        return metadata
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return {}


def _per_query_request_ids(body):
    """Ids of a multi-question answer (per_query[i].search_request_id), in question order."""
    per_query = body.get('per_query')
    if not isinstance(per_query, list):
        return []
    found = []
    for entry in per_query[:SESH_MAX_REQUEST_IDS]:
        value = entry.get('search_request_id') if isinstance(entry, dict) else None
        if isinstance(value, str) and SESH_UUID_PATTERN.fullmatch(value) is not None:
            found.append(value)
    return found


def search_receipt_metadata(payload):
    if not isinstance(payload, dict) or not is_sesh_search_tool(payload.get('tool_name')):
        return {}
    response = payload.get('tool_response')
    if not isinstance(response, dict):
        return {}
    # Traverse only exact bounded handle fields; never passages or tool input.
    bodies = _sesh_response_bodies(response)
    if bodies is None:
        return {}
    ids = set()
    all_ids = []
    delivered_sets = []
    delivered_invalid = False
    for body in bodies:
        value = body.get('search_request_id')
        per_query = _per_query_request_ids(body)
        if value is None and not per_query:
            continue
        if value is not None:
            if not isinstance(value, str) or SESH_UUID_PATTERN.fullmatch(value) is None:
                return {}
            ids.add(value)
        for item in ([value] if value is not None else []) + per_query:
            if item not in all_ids:
                all_ids.append(item)
        if value is None:
            continue
        try:
            delivered = _delivered_source_handle_digests(body)
            if delivered is not None:
                delivered_sets.append(delivered)
        except (ValueError, TypeError, UnicodeError, RecursionError):
            delivered_invalid = True
    # Conflicting envelopes are ambiguous, never choose whichever appears first.
    if len(ids) > 1 or not all_ids:
        return {}
    primary = ids.pop() if ids else all_ids[0]
    ordered = [primary] + [item for item in all_ids if item != primary]
    metadata = {'sesh_request_id': primary,
                'sesh_request_ids': ordered[:SESH_MAX_REQUEST_IDS]}
    if (not delivered_invalid and delivered_sets and
            all(item == delivered_sets[0] for item in delivered_sets[1:])):
        metadata['sesh_delivered_source_handle_sha256_v1'] = delivered_sets[0]
    return metadata



# --- Claude Code specifics -----------------------------------------------------------------------

def claude_tool_response(response):
    """Claude Code hands MCP results to hooks as a string, a list of content blocks or an object.
    Put them in the MCP envelope the receipt reader expects; never keeps or reads anything else."""
    if isinstance(response, dict):
        return response
    if isinstance(response, str):
        return {"content": [{"type": "text", "text": response}]}
    if isinstance(response, list):
        return {"content": response}
    return None


def search_metadata(payload: JsonDict) -> JsonDict:
    if not isinstance(payload, dict):
        return {}
    normalized = dict(payload)
    normalized["tool_response"] = claude_tool_response(payload.get("tool_response"))
    try:
        return search_receipt_metadata(normalized)
    except (ValueError, TypeError, UnicodeError, RecursionError):
        return {}


def tool_success(payload: JsonDict) -> bool | None:
    if payload.get("hook_event_name") == "PostToolUseFailure":
        return False
    response = payload.get("tool_response")
    if isinstance(response, dict) and response.get("isError") is True:
        return False
    return True if payload.get("hook_event_name") == "PostToolUse" else None


# --- timing --------------------------------------------------------------------------------------

def _timing_path(payload: JsonDict, base: Path) -> Path | None:
    call = payload.get("tool_use_id")
    sid = payload.get("session_id")
    if not isinstance(call, str) or not call or not isinstance(sid, str) or not sid:
        return None
    return base / "timing" / f"{digest(sid)}.{digest(call)}"


def mark_started(payload: JsonDict, base: Path | None = None) -> None:
    path = _timing_path(payload, base or state_dir())
    if path is None:
        return
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.write_text(str(time.time()), encoding="utf-8")
    _prune_old(path.parent)


def duration_ms(payload: JsonDict, base: Path | None = None) -> int | None:
    path = _timing_path(payload, base or state_dir())
    started = None
    if path is not None:
        try:
            started = float(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            started = None
        with contextlib.suppress(OSError):
            path.unlink()
    reported = payload.get("duration_ms")
    if isinstance(reported, (int, float)) and not isinstance(reported, bool) and reported >= 0:
        return int(reported)
    if started is None:
        return None
    return max(0, int((time.time() - started) * 1000))


# --- events --------------------------------------------------------------------------------------

def plugin_location(root=None) -> JsonDict:
    """Marketplace and version from the installed path .../plugins/cache/<marketplace>/sesh/<version>/."""
    root = root or os.environ.get("CLAUDE_PLUGIN_ROOT") or str(Path(__file__).resolve().parents[1])
    parts = Path(root).parts
    marketplace = None
    if "cache" in parts:
        index = len(parts) - 1 - parts[::-1].index("cache")
        if index + 2 < len(parts) and parts[index + 2] == PLUGIN_NAME:
            marketplace = parts[index + 1]
    return {"marketplace": marketplace or "local", "version": PLUGIN_VERSION}


def environment_metadata(payload: JsonDict) -> JsonDict:
    location = plugin_location()
    # The ingest keeps codex_setup.skills[{name, source, marketplace, plugin, version}] today, so
    # each session (and every Sesh search in it) maps to this plugin's version with no server change.
    return {"codex_setup": {
        "skills": [{"name": SKILL_NAME, "source": "plugin", "plugin": PLUGIN_NAME,
                    "marketplace": location["marketplace"], "version": PLUGIN_VERSION}],
        "plugins": [{"name": f"{PLUGIN_NAME}@{location['marketplace']}", "enabled": True}],
    }}


def tool_metadata(payload: JsonDict, phase: str) -> JsonDict:
    metadata: JsonDict = {"tool_name": payload.get("tool_name"), "tool_phase": phase}
    call = payload.get("tool_use_id")
    if isinstance(call, str) and call:
        metadata["tool_call_id"] = call
    return metadata


def capture(payload: JsonDict, *, event_type: str, hook_event: str, metadata: JsonDict | None = None,
            base: Path | None = None) -> JsonDict | None:
    """Queue one metadata-only event (and start a background upload). None when gated off."""
    if not linking_enabled() or not should_capture(payload):
        return None
    sid = payload.get("session_id")
    if not isinstance(sid, str) or not sid:
        return None
    base = base or telemetry_dir()
    session = safe_segment(sid)
    seq = SEQ_OFFSET + next_sequence(base, session)
    combined: JsonDict = {"platform": PLATFORM, "agent": AGENT}
    cwd = payload.get("cwd")
    if isinstance(cwd, str) and cwd:
        combined["cwd"] = cwd
    for key in ("transcript_path", "model", "source", "permission_mode"):
        value = payload.get(key)
        if isinstance(value, str) and value:
            combined[key] = value
    combined.update(arm_metadata(payload))
    if payload.get("agent_id"):
        combined["sesh_subagent"] = True
    combined.update(metadata or {})
    event_id = uuid.uuid4().hex
    created_at = datetime.now(timezone.utc).isoformat()
    storage_path = f"users/local/sessions/{session}/events/{seq:06d}-{safe_segment(event_type)}.json"
    detail = {"id": event_id, "session_id": session, "turn_id": None, "seq": seq,
              "event_type": event_type, "hook_event_name": hook_event, "created_at": created_at,
              "metadata": combined}
    write_json_atomic(base / storage_path, detail)
    record = {"id": event_id, "type": "event", "session_id": session, "turn_id": None, "seq": seq,
              "event_type": event_type, "hook_event_name": hook_event, "storage_bucket": DEFAULT_BUCKET,
              "storage_path": storage_path, "local_content_path": storage_path, "metadata": combined,
              "created_at": created_at, "uploaded_at": None}
    write_json_atomic(pending_queue_dir(base) / f"{safe_segment(event_id)}.json", record)
    try_auto_drain()
    return record


# --- queue and upload (same contract as claude-session-logging) ----------------------------------

def telemetry_dir() -> Path:
    return state_dir() / "session-logging"


def should_capture(payload: JsonDict) -> bool:
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    if not isinstance(cwd, str) or not cwd:
        return False
    return remote_belongs_to_org(git_origin_remote(cwd), ALLOWED_GITHUB_ORG)


def git_origin_remote(cwd: str) -> str | None:
    return _git_value(cwd, "remote", "get-url", "origin")


def _git_value(cwd: str, *args: str) -> str | None:
    try:
        result = subprocess.run(["git", "-C", cwd, *args], text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, check=False, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return None
    value = result.stdout.strip() if result.returncode == 0 else ""
    return value or None


def remote_belongs_to_org(remote: str | None, org: str) -> bool:
    if not remote:
        return False
    value = remote.strip()
    if re.match(rf"^https://github\.com/{re.escape(org)}/[^/]+?(?:\.git)?/?$", value, re.IGNORECASE):
        return True
    match = (re.match(rf"^git@(?P<host>[^:/\s]+):{re.escape(org)}/[^/]+?(?:\.git)?/?$", value, re.IGNORECASE)
             or re.match(rf"^ssh://git@(?P<host>[^/\s:]+)(?::\d+)?/{re.escape(org)}/[^/]+?(?:\.git)?/?$",
                         value, re.IGNORECASE))
    return bool(match) and ssh_host_resolves_to_github(match.group("host"))


@lru_cache(maxsize=32)
def ssh_host_resolves_to_github(host: str) -> bool:
    if host.rstrip(".").lower() == "github.com":
        return True
    try:
        result = subprocess.run(["ssh", "-G", host], text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.DEVNULL, check=False, timeout=2)
    except (OSError, subprocess.TimeoutExpired):
        return False
    for line in result.stdout.splitlines() if result.returncode == 0 else []:
        key, _, value = line.partition(" ")
        if key.lower() == "hostname":
            return value.strip().rstrip(".").lower() == "github.com"
    return False


def safe_segment(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in "._-" else "-" for char in value.strip())
    cleaned = "-".join(part for part in cleaned.split("-") if part)
    return cleaned or "unknown"


def next_sequence(base: Path, session: str) -> int:
    path = base / "sessions" / session / "sequence.txt"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(".lock").open("a", encoding="utf-8") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            try:
                current = int(path.read_text(encoding="utf-8").strip() or "0")
            except (OSError, ValueError):
                current = 0
            path.write_text(f"{current + 1}\n", encoding="utf-8")
            return current + 1
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def write_json_atomic(path: Path, payload: JsonDict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + f".{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(payload, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def pending_queue_dir(base: Path) -> Path:
    return base / "queue" / "pending"


def dead_letter_queue_dir(base: Path) -> Path:
    return base / "queue" / "dead-letter"


def auto_upload_enabled() -> bool:
    return not _falsy(os.environ.get(AUTO_UPLOAD_ENV, "1"))


def try_auto_drain() -> None:
    if not auto_upload_enabled():
        return
    with contextlib.suppress(Exception):  # Upload must never interrupt Claude Code.
        subprocess.Popen([sys.executable, str(Path(__file__).with_name("drain_queue.py"))],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         close_fds=True, start_new_session=True)


@contextlib.contextmanager
def drain_lock(base: Path):
    lock_path = base / "queue" / "drain.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        try:
            yield True
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def drain_queue(base: Path | None = None, post=None) -> JsonDict:
    base = base or telemetry_dir()
    base.mkdir(parents=True, exist_ok=True)
    post = post or post_payload
    uploaded = failed = dead = 0
    with drain_lock(base) as acquired:
        if not acquired:
            return {"locked": True}
        for path in sorted(pending_queue_dir(base).glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
                if time.time() - path.stat().st_mtime > MAX_EVENT_AGE_SECONDS:
                    # A late event would mark an ended session live again (the ingest clears
                    # ended_at on every event), so stale ones are dropped, not sent.
                    raise PermanentUploadError("event older than 30 minutes; not sent")
                post(build_ingest_payload(record, base=base))
            except PermanentUploadError as exc:
                dead += 1
                target = dead_letter_queue_dir(base) / path.name
                target.parent.mkdir(parents=True, exist_ok=True)
                with contextlib.suppress(OSError):
                    path.replace(target)
                append_error(base, exc)
            except Exception as exc:  # noqa: BLE001 - keep the record for the next drain.
                failed += 1
                append_error(base, exc)
            else:
                uploaded += 1
                path.unlink(missing_ok=True)
                with contextlib.suppress(OSError):
                    (base / str(record["local_content_path"])).unlink()
    return {"uploaded": uploaded, "failed": failed, "dead_lettered": dead,
            "remaining": len(list(pending_queue_dir(base).glob("*.json")))}


def append_error(base: Path, exc: Exception) -> None:
    with contextlib.suppress(OSError), (base / "upload_errors.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(),
                                 "error": str(exc)[:500]}) + "\n")


def build_ingest_payload(record: JsonDict, *, base: Path) -> JsonDict:
    detail = json.loads((base / str(record["local_content_path"])).read_text(encoding="utf-8"))
    payload = {"version": 1, "plugin": {"name": PLUGIN_NAME, "version": PLUGIN_VERSION},
               "record": record, "client": client_context(record, base=base), "event": detail}
    remote = payload["client"].get("repo_remote")
    if not remote_belongs_to_org(remote, ALLOWED_GITHUB_ORG):
        raise PermanentUploadError("client.repo_remote is outside the allowed GitHub org")
    return payload


def client_context(record: JsonDict, *, base: Path) -> JsonDict:
    """Who and where, exactly as claude-session-logging reports it, so both land on one user."""
    metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
    cwd = metadata.get("cwd") if isinstance(metadata.get("cwd"), str) else None
    git_email = _git_value(cwd, "config", "--get", "user.email") if cwd else None
    hostname = _quiet(socket.gethostname)
    username = _quiet(getpass.getuser)
    installation_id = local_installation_id(base)
    context: JsonDict = {
        "cwd": cwd,
        "repo_remote": git_origin_remote(cwd) if cwd else None,
        "git_email": git_email,
        "git_user_name": _git_value(cwd, "config", "--get", "user.name") if cwd else None,
        "git_branch": _git_value(cwd, "branch", "--show-current") if cwd else None,
        "hostname": hostname,
        "local_username": username,
        "installation_id": installation_id,
        "platform": PLATFORM,
    }
    if git_email:
        context["identity_key"] = f"git_email:{git_email.strip().lower()}"
    elif installation_id:
        context["identity_key"] = f"installation:{installation_id.strip()}"
    return {key: value for key, value in context.items() if value}


def _quiet(function):
    try:
        return function() or None
    except Exception:  # noqa: BLE001
        return None


def local_installation_id(base: Path) -> str:
    """claude-session-logging's installation id, at exactly its path, created there if missing.

    The ingest ties a session to the first installation id it sees, so this plugin and the
    logger must always send the same one.
    """
    override = os.environ.get("CLAUDE_SESSION_LOG_STATE_DIR")
    logger = Path(override).expanduser().resolve() if override else Path.home() / ".claude" / "session-logging"
    path = logger / "installation_id"
    with contextlib.suppress(OSError):
        value = path.read_text(encoding="utf-8").strip()
        if value:
            return value
    value = str(uuid.uuid4())
    with contextlib.suppress(OSError):
        write_text_atomic(path, f"{value}\n")
        return path.read_text(encoding="utf-8").strip() or value
    return value


def write_text_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


def post_payload(payload: JsonDict) -> None:
    headers = {"content-type": "application/json", "connection": "close",
               "user-agent": f"{PLUGIN_NAME}/{PLUGIN_VERSION} ({PLATFORM})"}
    token = os.environ.get(INGEST_TOKEN_ENV)
    if token:
        headers["x-codex-session-log-token"] = token
    url = os.environ.get(INGEST_URL_ENV) or DEFAULT_INGEST_URL
    request = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers=headers,
                                     method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30):
            return
    except urllib.error.HTTPError as exc:
        message = f"sesh ingest failed {exc.code}"
        if exc.code in PERMANENT_HTTP_STATUSES:
            raise PermanentUploadError(message) from exc
        raise RuntimeError(message) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"sesh ingest failed: {exc.reason}") from exc


class PermanentUploadError(RuntimeError):
    pass
