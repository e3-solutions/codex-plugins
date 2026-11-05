#!/usr/bin/env python3
from __future__ import annotations

import sys

from rollout_sync import sync_after_hook
from session_logging import capture_hook_event, read_stdin_json


def main() -> None:
    payload = read_stdin_json()
    try:
        capture_hook_event(payload, event_name="Stop")
        sync_after_hook(payload, event_name="Stop")
    except Exception as exc:  # noqa: BLE001 - logging must not interrupt Codex.
        print(f"codex-session-logging capture failed: {exc}", file=sys.stderr)

    # COR-5202: last, so capture and sync already ran. Prints a Stop "block"
    # decision only when the docs-maintenance flag is on; otherwise nothing.
    try:
        import docs_maintenance

        docs_maintenance.run(payload)
    except Exception as exc:  # noqa: BLE001 - must not interrupt Codex.
        print(f"codex-session-logging docs check failed: {exc}", file=sys.stderr)


if __name__ == "__main__":
    main()
