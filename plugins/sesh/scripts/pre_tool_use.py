#!/usr/bin/env python3
"""PreToolUse: the COR-4811 one-search-per-turn guard and a started event for Sesh tools, and the
COR-4887 existing-work (plan) cue for tool calls that create something new."""
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


def _print(output) -> None:
    try:
        if output:
            print(output, flush=True)
    except Exception:  # noqa: BLE001 - never break the tool call.
        pass


def sesh_tool(payload) -> None:
    denial = one_per_turn_denial(payload)
    if linking_enabled():
        try:
            from sesh_hooks import sesh_event_metadata

            extra = sesh_event_metadata(payload)  # COR-4887 arm.
            if denial:
                from sesh_need import denied_metadata

                extra = {**extra, **denied_metadata()}
            capture_hook_event(payload, event_name="PreToolUse", extra_metadata=extra or None)
        except Exception as exc:  # noqa: BLE001 - linking must not interrupt Codex.
            print(f"sesh linking failed: {exc}", file=sys.stderr)
    _print(denial)


def other_tool(payload) -> None:
    try:
        from plan_cue import plan_cue

        # Every apply_patch goes on (an update records its folder for the continuation rule);
        # other tools stop here. The logger shows its own cue when it runs.
        if payload.get("tool_name") != "apply_patch" or logger_runs("PreToolUse"):
            return
        output, moment = plan_cue(payload)  # COR-4887, on unless E3_SESH_PLAN_CUE=0/false/no/off.
    except Exception:  # noqa: BLE001 - the cue fails open.
        return
    if moment and linking_enabled():
        try:
            from sesh_hooks import capture_nudge

            capture_nudge(payload, moment, "PreToolUse")
        except Exception as exc:  # noqa: BLE001 - linking must not interrupt Codex.
            print(f"sesh linking failed: {exc}", file=sys.stderr)
    _print(output)


def main() -> None:
    try:
        payload = read_stdin_json()
        sesh = is_sesh_tool(payload.get("tool_name"))
        if sesh and logger_runs("PreToolUse"):
            return
    except Exception:  # noqa: BLE001 - never interrupt Codex.
        return
    if sesh:
        sesh_tool(payload)
    else:
        other_tool(payload)


if __name__ == "__main__":
    main()
