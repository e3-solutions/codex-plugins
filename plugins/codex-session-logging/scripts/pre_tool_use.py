#!/usr/bin/env python3
from __future__ import annotations

import sys

from session_logging import capture_hook_event, read_stdin_json


def one_per_turn_denial(payload) -> str | None:
    try:
        from sesh_need import one_per_turn_denial as denial

        return denial(payload)  # COR-4811, on unless E3_SESH_ONE_PER_TURN=0/false/no/off.
    except Exception:  # noqa: BLE001 - the guard fails open.
        return None


def plan_cue(payload):
    try:
        from plan_cue import plan_cue as cue

        return cue(payload)  # COR-4887, on unless E3_SESH_PLAN_CUE=0/false/no/off.
    except Exception:  # noqa: BLE001 - the cue fails open.
        return None, None


def main() -> None:
    denial = output = None
    try:
        payload = read_stdin_json()
        denial = one_per_turn_denial(payload)
        if not denial:
            output, moment = plan_cue(payload)
            if moment:
                try:
                    from sesh_hooks import capture_nudge

                    capture_nudge(payload, moment, "PreToolUse")
                except Exception as exc:  # noqa: BLE001 - logging must not interrupt Codex.
                    print(f"codex-session-logging capture failed: {exc}", file=sys.stderr)
        extra = {}
        try:
            from sesh_hooks import sesh_event_metadata

            extra = sesh_event_metadata(payload)  # COR-4887 arm on Sesh tool events only.
        except Exception:  # noqa: BLE001
            extra = {}
        if denial:
            from sesh_need import denied_metadata

            extra = {**extra, **denied_metadata()}
        capture_hook_event(payload, event_name="PreToolUse", extra_metadata=extra or None)
    except Exception as exc:  # noqa: BLE001 - logging must not interrupt Codex.
        print(f"codex-session-logging capture failed: {exc}", file=sys.stderr)
    try:
        if denial or output:
            print(denial or output, flush=True)
    except Exception:  # noqa: BLE001 - never break the tool call.
        pass


if __name__ == "__main__":
    main()
