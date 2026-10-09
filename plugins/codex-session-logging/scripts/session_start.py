#!/usr/bin/env python3
from __future__ import annotations

import sys
import os

from collective import session_context
from rollout_sync import sync_after_hook
from session_logging import capture_hook_event, read_stdin_json, should_prompt_collective


def main() -> None:
    payload = read_stdin_json()
    experiment = None
    try:
        from sesh_context import sesh_cue_decision

        context, experiment = sesh_cue_decision(payload)
        if context:
            print(context, flush=True)
    except Exception:
        pass  # Retrieval guidance must not interrupt existing hooks.
    preview = os.environ.get("E3_COLLECTIVE_FEEDBACK_HOOK_ENABLED", "1") == "1"
    if preview:
        try:
            from collective_feedback import feedback_context

            context = feedback_context(payload, event_name="SessionStart", agent="codex")
            if context:
                print(context, flush=True)
        except Exception:
            pass  # An incomplete preview package must not stop telemetry.
    try:
        if experiment:  # COR-4681 arm, only while E3_SESH_CUE_EXPERIMENT is on.
            capture_hook_event(payload, event_name="SessionStart", extra_metadata=experiment)
        else:
            capture_hook_event(payload, event_name="SessionStart")
        sync_after_hook(payload, event_name="SessionStart")
    except Exception as exc:  # noqa: BLE001 - logging must not interrupt Codex.
        print(f"codex-session-logging capture failed: {exc}", file=sys.stderr)
    try:
        from cosmos_health import report_cosmos_health

        report_cosmos_health(payload)
    except Exception:  # noqa: BLE001 - the health signal must not interrupt Codex.
        pass
    try:
        context = None if preview else session_context(eligible=should_prompt_collective(payload))
        if context:
            print(context)
    except Exception as exc:  # noqa: BLE001 - guidance must not interrupt Codex.
        print(f"collective context failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
