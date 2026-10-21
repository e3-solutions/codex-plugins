#!/usr/bin/env python3
from __future__ import annotations

import sys

from rollout_sync import sync_after_hook
from session_logging import capture_hook_event, read_stdin_json


def main() -> None:
    try:
        payload = read_stdin_json()
    except Exception as exc:  # noqa: BLE001 - logging must not interrupt Codex.
        print(f"codex-session-logging capture failed: {exc}", file=sys.stderr)
        return
    cue = forum_meta = nudge = None
    try:
        from forum_decision_cue import new_turn

        new_turn(payload)  # COR-4911: the decision-point Forum cue counts tool calls per turn.
    except Exception:  # noqa: BLE001
        pass
    try:
        from forum_cue import forum_cue_decision

        cue, forum_meta = forum_cue_decision(payload)  # COR-4883 arm only while FORUM_CUE_AB is on.
    except Exception:
        pass  # Forum guidance must not interrupt logging or the user's prompt.
    try:
        from sesh_need import with_past_work_nudge

        # COR-4811, on unless E3_SESH_PAST_WORK_CUE=0/false/no/off; COR-4887 exposure labels.
        cue, nudge = with_past_work_nudge(cue, payload)
    except Exception:
        nudge = None  # Sesh guidance must not interrupt logging or the user's prompt.
    try:
        if cue:
            print(cue, flush=True)
    except Exception:
        pass
    if nudge:
        try:
            from sesh_hooks import capture_nudge

            capture_nudge(payload, nudge, "UserPromptSubmit")
        except Exception as exc:  # noqa: BLE001 - logging must not interrupt Codex.
            print(f"codex-session-logging capture failed: {exc}", file=sys.stderr)
    try:
        if forum_meta:
            capture_hook_event(payload, event_name="UserPromptSubmit", extra_metadata=forum_meta)
        else:
            capture_hook_event(payload, event_name="UserPromptSubmit")
        sync_after_hook(payload, event_name="UserPromptSubmit")
    except Exception as exc:  # noqa: BLE001 - logging must not interrupt Codex.
        print(f"codex-session-logging capture failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
