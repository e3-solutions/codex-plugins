#!/usr/bin/env python3
"""PreToolUse: the one-search-per-turn guard on Sesh search, a started event for Sesh tools, and the
plan cue before Write creates a new code file. Never blocks anything but a second search in a turn."""
from __future__ import annotations

import sys


def sesh_tool(payload) -> None:
    from sesh_cc import DENIED_METADATA, one_per_turn_denial
    from sesh_linking import capture, mark_started, tool_metadata

    denial = one_per_turn_denial(payload)  # Fails open.
    try:
        mark_started(payload)
        metadata = tool_metadata(payload, "started")
        if denial:
            metadata.update(DENIED_METADATA)
        capture(payload, event_type="tool_call_started", hook_event="PreToolUse", metadata=metadata)
    except Exception as exc:  # noqa: BLE001 - linking must not interrupt Claude Code.
        print(f"sesh linking failed: {exc}", file=sys.stderr)
    if denial:
        print(denial, flush=True)


def write_tool(payload) -> None:
    from sesh_cc import hook_output, plan_cue_decision

    cue, metadata = plan_cue_decision(payload)
    if metadata:
        try:
            from sesh_linking import capture

            event_type = "sesh_plan_cue_withheld" if metadata["sesh_plan_cue"] == "withheld" else "sesh_plan_cue_shown"
            capture(payload, event_type=event_type, hook_event="PreToolUse", metadata=metadata)
        except Exception as exc:  # noqa: BLE001
            print(f"sesh linking failed: {exc}", file=sys.stderr)
    if cue:
        print(hook_output("PreToolUse", additionalContext=cue), flush=True)


def main() -> None:
    try:
        from sesh_cc import is_sesh_tool
        from sesh_linking import read_stdin_json

        payload = read_stdin_json()
        name = payload.get("tool_name")
        if is_sesh_tool(name):
            sesh_tool(payload)
        elif name == "Write":
            write_tool(payload)
    except Exception:  # noqa: BLE001 - never interrupt or block Claude Code.
        pass


if __name__ == "__main__":
    main()
