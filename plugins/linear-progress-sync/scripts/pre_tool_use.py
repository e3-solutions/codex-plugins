#!/usr/bin/env python3
from __future__ import annotations

import sys

from linear_sync import pre_tool_guard_decision, read_stdin_json


def main() -> None:
    decision = pre_tool_guard_decision(read_stdin_json())
    if decision.blocked:
        print(decision.message, file=sys.stderr)
        raise SystemExit(2)


if __name__ == "__main__":
    main()
