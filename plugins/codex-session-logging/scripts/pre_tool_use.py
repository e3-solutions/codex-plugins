#!/usr/bin/env python3
from __future__ import annotations

import sys

from session_logging import capture_hook_event, read_stdin_json


def one_per_turn_denial(payload) -> str | None:
    try:
        from sesh_need import one_per_turn_denial as denial

        return denial(payload)  # COR-4811, only while E3_SESH_ONE_PER_TURN is on.
    except Exception:  # noqa: BLE001 - the guard fails open.
        return None


def main() -> None:
    denial = None
    try:
        payload = read_stdin_json()
        denial = one_per_turn_denial(payload)
        if denial:
            from sesh_need import DENIED_METADATA

            capture_hook_event(payload, event_name="PreToolUse", extra_metadata=DENIED_METADATA)
        else:
            capture_hook_event(payload, event_name="PreToolUse")
    except Exception as exc:  # noqa: BLE001 - logging must not interrupt Codex.
        print(f"codex-session-logging capture failed: {exc}", file=sys.stderr)
    if denial:
        try:
            print(denial, flush=True)
        except Exception:  # noqa: BLE001 - never break the tool call.
            pass


if __name__ == "__main__":
    main()
