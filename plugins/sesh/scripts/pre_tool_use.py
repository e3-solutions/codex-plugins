#!/usr/bin/env python3
"""PreToolUse: the COR-4811 one-search-per-turn guard, and a started event for Sesh tools only."""
from __future__ import annotations

import sys

from sesh_plugin import is_sesh_tool, linking_enabled, logger_runs
from session_logging import capture_hook_event, read_stdin_json


def one_per_turn_denial(payload) -> str | None:
    try:
        from sesh_need import one_per_turn_denial as denial

        return denial(payload)  # COR-4811, on unless E3_SESH_ONE_PER_TURN=0/false/no/off.
    except Exception:  # noqa: BLE001 - the guard fails open.
        return None


def main() -> None:
    try:
        payload = read_stdin_json()
        if not is_sesh_tool(payload.get("tool_name")) or logger_runs("PreToolUse"):
            return
    except Exception:  # noqa: BLE001 - never interrupt Codex.
        return
    denial = one_per_turn_denial(payload)
    if linking_enabled():
        try:
            if denial:
                from sesh_need import DENIED_METADATA

                capture_hook_event(payload, event_name="PreToolUse", extra_metadata=DENIED_METADATA)
            else:
                capture_hook_event(payload, event_name="PreToolUse")
        except Exception as exc:  # noqa: BLE001 - linking must not interrupt Codex.
            print(f"sesh linking failed: {exc}", file=sys.stderr)
    if denial:
        try:
            print(denial, flush=True)
        except Exception:  # noqa: BLE001 - never break the tool call.
            pass


if __name__ == "__main__":
    main()
