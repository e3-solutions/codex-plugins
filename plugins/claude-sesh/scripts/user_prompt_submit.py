#!/usr/bin/env python3
"""UserPromptSubmit: start a new turn for the one-search guard, and the past-work cue.

The prompt is matched locally against the past-work pattern and never stored or sent.
"""
from __future__ import annotations


def main() -> None:
    try:
        from sesh_cc import hook_output, past_work_cue, start_turn
        from sesh_linking import read_stdin_json

        payload = read_stdin_json()
    except Exception:  # noqa: BLE001 - never change or break the prompt.
        return
    try:
        start_turn(payload)
    except Exception:  # noqa: BLE001 - without a turn the guard simply allows every search.
        pass
    try:
        cue = past_work_cue(payload)
        if cue:
            print(hook_output("UserPromptSubmit", additionalContext=cue), flush=True)
    except Exception:  # noqa: BLE001
        pass


if __name__ == "__main__":
    main()
