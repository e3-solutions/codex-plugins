#!/usr/bin/env python3
"""SessionStart: the Sesh startup cue (with the COR-4808 routine A/B) and one environment snapshot."""
from __future__ import annotations

import sys

from sesh_plugin import linking_enabled, logger_runs
from session_logging import capture_hook_event, read_stdin_json


def main() -> None:
    try:
        payload = read_stdin_json()
        if logger_runs("SessionStart"):
            return  # codex-session-logging shows the cue and logs the session itself.
    except Exception:  # noqa: BLE001 - never interrupt Codex.
        return
    experiment = nudge = None
    try:
        from sesh_context import startup_nudge

        context, experiment, nudge = startup_nudge(payload)
        if context:
            print(context, flush=True)
    except Exception:
        pass  # Retrieval guidance must not interrupt Codex.
    if not linking_enabled():
        return
    try:
        # codex_setup in this snapshot lists the installed skills with plugin and version
        # (search-coding-sessions from plugin "sesh"), which is how searches in this session
        # are tied to the sesh plugin version. COR-4681 arm only while that experiment is on.
        try:
            from sesh_hooks import snapshot_metadata

            labels = snapshot_metadata(payload, experiment)  # Plus the COR-4887 sesh_arm.
        except Exception:  # noqa: BLE001
            labels = experiment
        capture_hook_event(payload, event_name="SessionStart", extra_metadata=labels or None)
        if nudge:
            from sesh_hooks import capture_nudge

            capture_nudge(payload, nudge, "SessionStart")  # COR-4887 exposure: shown or withheld.
    except Exception as exc:  # noqa: BLE001 - linking must not interrupt Codex.
        print(f"sesh linking failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
