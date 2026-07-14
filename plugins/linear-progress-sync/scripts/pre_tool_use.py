#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path

from linear_sync import pre_tool_guard_decision, read_stdin_json


def main() -> None:
    try:
        from resident_updater import ensure_resident_updater

        ensure_resident_updater(Path(__file__).resolve().parents[1])
    except Exception:
        pass
    decision = pre_tool_guard_decision(read_stdin_json())
    if decision.blocked:
        print(decision.message, file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
