#!/usr/bin/env python3
"""UserPromptSubmit: the COR-4811 past-work cue. The prompt is never stored or sent."""
from __future__ import annotations

from sesh_plugin import logger_runs
from session_logging import read_stdin_json


def main() -> None:
    try:
        payload = read_stdin_json()
        if logger_runs("UserPromptSubmit"):
            return
        from sesh_need import with_past_work_cue

        cue = with_past_work_cue(None, payload)  # On unless E3_SESH_PAST_WORK_CUE=0/false/no/off.
        if cue:
            print(cue, flush=True)
    except Exception:  # noqa: BLE001 - the cue must never change or break the prompt.
        pass


if __name__ == "__main__":
    main()
