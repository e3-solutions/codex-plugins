#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import time

from linear_sync import cli_root_arg, drain_once


def main() -> None:
    parser = argparse.ArgumentParser(description="Drain queued Linear progress sync events.")
    parser.add_argument("--once", action="store_true", help="Process the queue once and exit.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write to Linear; record decisions locally.")
    parser.add_argument("--interval", type=float, default=30.0, help="Polling interval when not using --once.")
    cli_root_arg(parser)
    args = parser.parse_args()

    while True:
        result = drain_once(root=args.root, dry_run=args.dry_run)
        print(json.dumps(result, sort_keys=True))
        if args.once:
            return
        time.sleep(max(args.interval, 1.0))


if __name__ == "__main__":
    main()

