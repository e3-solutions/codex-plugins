#!/usr/bin/env python3
from __future__ import annotations

import json
import sys

from rollout_sync import sync_after_hook
from session_logging import capture_hook_event, read_stdin_json


def prompt_contexts(payload) -> list[str]:
    """Deferred Sesh cue (person's first prompt only) then the Forum cue."""
    contexts: list[str] = []
    try:
        import sesh_gate
        from sesh_context import sesh_prompt_context

        state = sesh_gate.start_turn(payload)
        cue = sesh_prompt_context(payload, state)
        if cue:
            contexts.append(cue)
    except Exception:
        pass  # Sesh guidance must not interrupt logging or the user's prompt.
    try:
        from forum_cue import forum_cue

        cue = forum_cue(payload)
        if cue:
            contexts.append(json.loads(cue)["hookSpecificOutput"]["additionalContext"])
    except Exception:
        pass  # Forum guidance must not interrupt logging or the user's prompt.
    return contexts


def main() -> None:
    try:
        payload = read_stdin_json()
    except Exception as exc:  # noqa: BLE001 - logging must not interrupt Codex.
        print(f"codex-session-logging capture failed: {exc}", file=sys.stderr)
        return
    contexts = prompt_contexts(payload)
    if contexts:
        print(json.dumps({"hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": "\n\n".join(contexts),
        }}), flush=True)
    try:
        capture_hook_event(payload, event_name="UserPromptSubmit")
        sync_after_hook(payload, event_name="UserPromptSubmit")
    except Exception as exc:  # noqa: BLE001 - logging must not interrupt Codex.
        print(f"codex-session-logging capture failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
