#!/usr/bin/env python3
"""Transcript-driven capture of Claude Code prompt/response content + token usage.

Unlike the metadata-only presence/lifecycle path, this module reads the Claude
Code transcript (``~/.claude/projects/<slug>/<sessionId>.jsonl``) — the source of
truth for a session — and emits full-body ``message`` records and per-turn
``usage`` records through the plugin's existing queue, mirroring
``codex-session-logging``. This is the approved FULL CODEX PARITY behavior: the
prompt/response/tool bodies are stored so the shared ingest can populate
``codex_session_messages`` and ``codex_session_usage`` for Claude sessions, making
the codestat ``token_usage_by_agent`` view agent-symmetric (COR-2786 / ATC).

Idempotency: every record id is deterministic (derived from the transcript line
``uuid``) so re-emits dedupe via the ingest's ``on_conflict=id``, and a per-session
cursor persisted in the plugin state dir means only new transcript lines are
processed. Scope is gated to e3-solutions repos exactly like the rest of the
plugin (via ``publish_presence.session_target``). Everything is tagged
``metadata.agent = "claude"``.
"""
from __future__ import annotations

import fcntl
import json
from pathlib import Path
from typing import Any, Iterator

import publish_presence
import session_logging

JsonDict = dict[str, Any]

SYNC_VERSION = 1
# Live records must NOT be tagged historical_transcript, or the shared ingest
# drops them (see ``isHistoricalBackfill`` in the edge function). Use a distinct
# source so the message/usage rows are written server-side.
SYNC_SOURCE = "transcript_sync"

# Best-effort model -> context window map so codex_session_usage.model_context_window
# is populated when we recognise the model. Unknown models simply omit the field.
MODEL_CONTEXT_WINDOWS: tuple[tuple[str, int], ...] = (
    ("claude-opus-4", 200000),
    ("claude-sonnet-4", 200000),
    ("claude-3-5-sonnet", 200000),
    ("claude-3-5-haiku", 200000),
    ("claude-3-opus", 200000),
    ("claude-3-sonnet", 200000),
    ("claude-3-haiku", 200000),
    ("claude-sonnet", 200000),
    ("claude-opus", 200000),
    ("claude-haiku", 200000),
)


def sync_from_hook(payload: JsonDict) -> JsonDict:
    """Entry point for the Stop / SessionEnd hooks: pull ids from the hook payload."""
    session_id = session_logging.first_string(payload, "session_id", "sessionId")
    transcript_path = session_logging.first_string(payload, "transcript_path", "transcriptPath")
    if not session_id or not transcript_path:
        return {"synced": 0, "reason": "missing_transcript"}
    return sync_transcript_records(session_id, transcript_path)


def sync_transcript_records(
    session_id: str,
    transcript_path: str | Path,
    *,
    base: Path | None = None,
    auto_drain: bool = True,
    source: str = SYNC_SOURCE,
    cursor_scope: str = "live",
) -> JsonDict:
    """Emit message + usage records for transcript lines not yet processed.

    Reads a per-session cursor (last processed line index), processes only newer
    lines, emits one ``message`` record per user/assistant/tool turn and one
    ``usage`` record per assistant turn carrying ``message.usage``, then persists
    the cursor. Deterministic ids + cursor make re-runs a no-op.

    ``source`` stamps ``metadata.source`` (live sync uses ``transcript_sync`` so
    the ingest writes the rows; the historical backfill passes
    ``historical_transcript`` to match the drop-until-enabled posture of the rest
    of the backfill). ``cursor_scope`` keeps the live and backfill cursors
    independent so a backfill pass over an active session never suppresses live
    capture — deterministic ids still dedupe any overlap at the ingest.
    """
    path = Path(transcript_path).expanduser()
    if not path.exists():
        return {"synced": 0, "reason": "missing_transcript"}
    target = publish_presence.session_target(path)
    if target is None:
        return {"synced": 0, "reason": "not_eligible"}
    # session_target derives the id from the filename; prefer the caller's id
    # (the live session id from the hook) so live + backfill agree.
    target["session_id"] = session_id or target["session_id"]

    base = base or session_logging.ensure_state_dir()
    safe_session = session_logging.safe_segment(str(target["session_id"]))
    cursor_key = f"{cursor_scope}-{safe_session}" if cursor_scope != "live" else safe_session

    lock_path = _cursor_path(base, cursor_key).with_suffix(".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"synced": 0, "reason": "locked"}

        cursor = _read_cursor(base, cursor_key)
        last_index = int(cursor.get("index", -1)) if isinstance(cursor.get("index"), (int, float)) else -1

        messages = 0
        usage_count = 0
        queued = 0
        highest_index = last_index
        last_uuid = cursor.get("uuid")

        for index, envelope in _iter_transcript(path):
            highest_index = max(highest_index, index)
            if index <= last_index:
                continue
            line_uuid = _string(envelope.get("uuid")) or f"line-{index}"
            last_uuid = line_uuid

            message_record = _message_record(
                envelope,
                target=target,
                base=base,
                safe_session=safe_session,
                seq=index,
                line_uuid=line_uuid,
                source=source,
            )
            if message_record is not None:
                session_logging.enqueue_record(base, message_record)
                messages += 1
                queued += 1

            usage_record = _usage_record(
                envelope,
                target=target,
                base=base,
                safe_session=safe_session,
                seq=index,
                line_uuid=line_uuid,
                source=source,
            )
            if usage_record is not None:
                session_logging.enqueue_record(base, usage_record)
                usage_count += 1
                queued += 1

        if highest_index > last_index:
            _write_cursor(base, cursor_key, {"index": highest_index, "uuid": last_uuid})

    if queued and auto_drain:
        session_logging.try_auto_drain()

    return {
        "synced": queued,
        "messages": messages,
        "usage": usage_count,
        "queued": queued,
        "session_id": target["session_id"],
    }


# --- record builders -------------------------------------------------------


def _message_record(
    envelope: JsonDict,
    *,
    target: JsonDict,
    base: Path,
    safe_session: str,
    seq: int,
    line_uuid: str,
    source: str = SYNC_SOURCE,
) -> JsonDict | None:
    role, content = _role_and_content(envelope)
    if role is None or not content:
        return None
    session_id = str(target["session_id"])
    transcript_path = str(target["transcript_path"])
    content_bytes = content.encode("utf-8")
    content_hash = session_logging.sha256_hex(content)
    storage_path = f"users/local/sessions/{safe_session}/messages/{seq:06d}-{role}.json"
    created_at = _string(envelope.get("timestamp")) or session_logging.now_iso()
    metadata = _base_metadata(target, source)
    metadata["role"] = role
    record_id = _record_id(session_id, line_uuid, "message")
    thread_id = session_logging.sha256_hex(transcript_path)

    detail: JsonDict = {
        "id": record_id,
        "session_id": session_id,
        "thread_id": thread_id,
        "turn_id": None,
        "seq": seq,
        "role": role,
        "content": content,
        "content_sha256": content_hash,
        "content_byte_size": len(content_bytes),
        "hook_event_name": "TranscriptSync",
        "created_at": created_at,
        "metadata": metadata,
    }
    session_logging.write_json_atomic(base / storage_path, detail)

    record: JsonDict = {
        "id": record_id,
        "type": "message",
        "session_id": session_id,
        "thread_id": thread_id,
        "turn_id": None,
        "seq": seq,
        "role": role,
        "hook_event_name": "TranscriptSync",
        "storage_bucket": session_logging.bucket_name(),
        "storage_path": storage_path,
        "local_content_path": storage_path,
        "content_sha256": content_hash,
        "content_byte_size": len(content_bytes),
        "content_excerpt": session_logging.utf8_excerpt(content_bytes),
        "metadata": metadata,
        "created_at": created_at,
        "uploaded_at": None,
    }
    return record


def _usage_record(
    envelope: JsonDict,
    *,
    target: JsonDict,
    base: Path,
    safe_session: str,
    seq: int,
    line_uuid: str,
    source: str = SYNC_SOURCE,
) -> JsonDict | None:
    if _string(envelope.get("type")) != "assistant":
        return None
    message = envelope.get("message")
    if not isinstance(message, dict):
        return None
    usage = message.get("usage")
    if not isinstance(usage, dict):
        return None

    input_tokens = _non_negative_int(usage.get("input_tokens"))
    output_tokens = _non_negative_int(usage.get("output_tokens"))
    if input_tokens is None or output_tokens is None:
        return None
    cache_read = _non_negative_int(usage.get("cache_read_input_tokens")) or 0
    cache_creation = _non_negative_int(usage.get("cache_creation_input_tokens")) or 0
    total_tokens = input_tokens + output_tokens + cache_creation + cache_read

    session_id = str(target["session_id"])
    transcript_path = str(target["transcript_path"])
    created_at = _string(envelope.get("timestamp")) or session_logging.now_iso()
    model = _string(message.get("model"))
    service_tier = _string(usage.get("service_tier"))

    metadata = _base_metadata(target, source)
    if model:
        metadata["model"] = model
    if service_tier:
        metadata["service_tier"] = service_tier
    metadata["cache_creation_input_tokens"] = cache_creation

    record_id = _record_id(session_id, line_uuid, "usage")
    thread_id = session_logging.sha256_hex(transcript_path)
    storage_path = f"users/local/sessions/{safe_session}/usage/{seq:06d}.json"

    detail: JsonDict = {
        "id": record_id,
        "session_id": session_id,
        "thread_id": thread_id,
        "input_tokens": input_tokens,
        "cached_input_tokens": cache_read,
        "output_tokens": output_tokens,
        "reasoning_output_tokens": 0,
        "total_tokens": total_tokens,
        "created_at": created_at,
        "metadata": metadata,
    }
    context_window = _model_context_window(model)
    if context_window is not None:
        detail["model_context_window"] = context_window
    session_logging.write_json_atomic(base / storage_path, detail)

    record: JsonDict = {
        "id": record_id,
        "type": "usage",
        "session_id": session_id,
        "thread_id": thread_id,
        "seq": seq,
        "created_at": created_at,
        "metadata": metadata,
        "storage_bucket": session_logging.bucket_name(),
        "storage_path": storage_path,
        "local_content_path": storage_path,
        "uploaded_at": None,
    }
    return record


# --- transcript parsing ----------------------------------------------------


def _role_and_content(envelope: JsonDict) -> tuple[str | None, str]:
    line_type = _string(envelope.get("type"))
    if line_type not in {"user", "assistant"}:
        return None, ""
    message = envelope.get("message")
    if not isinstance(message, dict):
        return None, ""
    raw = message.get("content")
    if line_type == "user" and _is_tool_result(raw):
        return "tool", _serialize_content(raw)
    return line_type, _serialize_content(raw)


def _is_tool_result(content: object) -> bool:
    if not isinstance(content, list):
        return False
    has_tool_result = any(
        isinstance(block, dict) and block.get("type") == "tool_result" for block in content
    )
    if not has_tool_result:
        return False
    # A user turn that mixes real text with tool_result is still a user prompt.
    has_text = any(
        isinstance(block, dict)
        and block.get("type") == "text"
        and isinstance(block.get("text"), str)
        and block["text"].strip()
        for block in content
    )
    return not has_text


def _serialize_content(content: object) -> str:
    """Faithfully serialize Claude message content into a stored text body.

    Text/thinking blocks are joined as-is; tool_use / tool_result blocks are
    rendered with a header + JSON body so nothing is lost (FULL CODEX PARITY).
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else json.dumps(content, ensure_ascii=False, sort_keys=True)

    parts: list[str] = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
            continue
        if not isinstance(block, dict):
            parts.append(json.dumps(block, ensure_ascii=False, sort_keys=True))
            continue
        block_type = block.get("type")
        if block_type in {"text", "output_text", "input_text"} and isinstance(block.get("text"), str):
            parts.append(block["text"])
        elif block_type == "thinking" and isinstance(block.get("thinking"), str):
            parts.append(block["thinking"])
        elif block_type == "tool_use":
            name = block.get("name")
            header = f"[tool_use name={name} id={block.get('id')}]"
            parts.append(header + "\n" + json.dumps(block.get("input"), ensure_ascii=False, sort_keys=True))
        elif block_type == "tool_result":
            header = f"[tool_result tool_use_id={block.get('tool_use_id')} is_error={block.get('is_error', False)}]"
            body = block.get("content")
            if isinstance(body, str):
                parts.append(header + "\n" + body)
            else:
                parts.append(header + "\n" + json.dumps(body, ensure_ascii=False, sort_keys=True))
        else:
            parts.append(json.dumps(block, ensure_ascii=False, sort_keys=True))
    return "\n".join(part for part in parts if part is not None)


def _iter_transcript(path: Path) -> Iterator[tuple[int, JsonDict]]:
    with path.open(encoding="utf-8") as handle:
        for index, line in enumerate(handle):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                loaded = json.loads(stripped)
            except json.JSONDecodeError:
                continue
            if isinstance(loaded, dict):
                yield index, loaded


# --- helpers ---------------------------------------------------------------


def _base_metadata(target: JsonDict, source: str = SYNC_SOURCE) -> JsonDict:
    metadata: JsonDict = {
        "platform": session_logging.PLATFORM,
        "agent": session_logging.AGENT,
        "cwd": str(target["cwd"]),
        "transcript_path": str(target["transcript_path"]),
        "source": source,
    }
    branch = target.get("git_branch")
    if isinstance(branch, str) and branch:
        metadata["git_branch"] = branch
    return metadata


def _record_id(session_id: str, line_uuid: str, kind: str) -> str:
    return session_logging.deterministic_uuid(
        f"claude-transcript-v{SYNC_VERSION}:{session_id}:{line_uuid}:{kind}"
    )


def _model_context_window(model: str | None) -> int | None:
    if not model:
        return None
    lowered = model.lower()
    for prefix, window in MODEL_CONTEXT_WINDOWS:
        if prefix in lowered:
            return window
    return None


def _string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _non_negative_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and value >= 0:
        return value
    if isinstance(value, float) and value.is_integer() and value >= 0:
        return int(value)
    return None


def _cursor_dir(base: Path) -> Path:
    return base / "transcript-sync" / f"v{SYNC_VERSION}"


def _cursor_path(base: Path, safe_session: str) -> Path:
    return _cursor_dir(base) / f"{safe_session}.json"


def _read_cursor(base: Path, safe_session: str) -> JsonDict:
    try:
        loaded = json.loads(_cursor_path(base, safe_session).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def _write_cursor(base: Path, safe_session: str, cursor: JsonDict) -> None:
    path = _cursor_path(base, safe_session)
    session_logging.write_json_atomic(path, {**cursor, "updated_at": session_logging.now_iso()})
