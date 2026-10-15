#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import re
import sys
import unicodedata

from session_logging import capture_hook_event, read_stdin_json

# COR-4786: pass the caller's own session id to Sesh as exclude_session_id so a
# live session never finds itself. Matches the same Sesh tool names as the
# logger (mcp__<ns>__sesh__<tool>, mcp__<ns>__<tool>, bare name) for
# search_coding_sessions and ask_sesh.
SESH_EXCLUDE_TOOL_PATTERN = re.compile(
    r"(?:[A-Za-z0-9_-]+__)?(?:sesh__)?(?:search_coding_sessions|ask_sesh)"
)
MAX_SESSION_ID_LENGTH = 256
EXCLUDE_SELF_ENV = "E3_SESH_EXCLUDE_SELF"
FALSE_VALUES = frozenset({"0", "false", "no", "off"})


def exclude_self_enabled() -> bool:
    return os.environ.get(EXCLUDE_SELF_ENV, "").strip().lower() not in FALSE_VALUES


def valid_session_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value.strip())
        and len(value) <= MAX_SESSION_ID_LENGTH
        and not any(unicodedata.category(char) == "Cc" for char in value)
    )


def sesh_exclude_hook_output(payload: object) -> dict | None:
    """Return the PreToolUse output that adds exclude_session_id, or None to do nothing."""
    if not exclude_self_enabled() or not isinstance(payload, dict):
        return None
    tool_name = payload.get("tool_name")
    if not isinstance(tool_name, str) or SESH_EXCLUDE_TOOL_PATTERN.fullmatch(tool_name) is None:
        return None
    session_id = payload.get("session_id")
    tool_input = payload.get("tool_input")
    if not valid_session_id(session_id) or not isinstance(tool_input, dict):
        return None
    if tool_input.get("exclude_session_id") is not None or tool_input.get("include_own_session") is not None:
        return None
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "allow",
            "updatedInput": {**tool_input, "exclude_session_id": session_id},
        }
    }


def main() -> None:
    payload = None
    try:
        payload = read_stdin_json()
        capture_hook_event(payload, event_name="PreToolUse")
    except Exception as exc:  # noqa: BLE001 - logging must not interrupt Claude Code.
        print(f"claude-session-logging capture failed: {exc}", file=sys.stderr)
    try:
        output = sesh_exclude_hook_output(payload)
        if output is not None:
            sys.stdout.write(json.dumps(output))
            sys.stdout.flush()
    except Exception:  # noqa: BLE001 - never break the tool call.
        pass


if __name__ == "__main__":
    main()
