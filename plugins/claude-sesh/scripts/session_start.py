#!/usr/bin/env python3
"""SessionStart: the Sesh startup cue (holdout, then routine A/B) and one environment snapshot."""
from __future__ import annotations

import sys


def main() -> None:
    try:
        from sesh_linking import read_stdin_json

        payload = read_stdin_json()
    except Exception:  # noqa: BLE001 - never interrupt Claude Code.
        return
    try:
        from sesh_cc import hook_output, startup_cue

        cue = startup_cue(payload)
        if cue:
            print(hook_output("SessionStart", additionalContext=cue), flush=True)
    except Exception:  # noqa: BLE001 - guidance must not interrupt Claude Code.
        pass
    try:
        from sesh_linking import capture, environment_metadata

        capture(payload, event_type="environment_snapshot", hook_event="SessionStart",
                metadata=environment_metadata(payload))
    except Exception as exc:  # noqa: BLE001 - linking must not interrupt Claude Code.
        print(f"sesh linking failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
