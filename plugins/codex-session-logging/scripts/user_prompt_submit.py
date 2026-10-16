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
    cue = None
    try:
        from forum_cue import forum_cue

        cue = forum_cue(payload)
    except Exception:
        pass  # Forum guidance must not interrupt logging or the user's prompt.
    try:
        from sesh_need import with_past_work_cue

        cue = with_past_work_cue(cue, payload)  # COR-4811, on unless E3_SESH_PAST_WORK_CUE=0/false/no/off.
    except Exception:
        pass  # Sesh guidance must not interrupt logging or the user's prompt.
    try:
        if cue:
            print(cue, flush=True)
    except Exception:
        pass
    try:
        capture_hook_event(payload, event_name="UserPromptSubmit")
        sync_after_hook(payload, event_name="UserPromptSubmit")
    except Exception as exc:  # noqa: BLE001 - logging must not interrupt Codex.
        print(f"codex-session-logging capture failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
