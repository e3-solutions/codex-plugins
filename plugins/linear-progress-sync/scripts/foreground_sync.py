#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from linear_sync import (
    ack_foreground_event,
    cli_root_arg,
    foreground_sync_plan,
    skip_foreground_event,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Foreground Linear progress sync helper.")
    sub = parser.add_subparsers(dest="command", required=True)

    prepare = sub.add_parser("prepare", help="Print queued events eligible for foreground Linear sync.")
    cli_root_arg(prepare)
    prepare.add_argument("--limit", type=int, default=5)

    ack = sub.add_parser("ack", help="Acknowledge an event after a Linear comment is visibly confirmed.")
    cli_root_arg(ack)
    ack.add_argument("--event-id", required=True)
    ack.add_argument("--issue-key", required=True)
    ack.add_argument("--commit-sha")

    skip = sub.add_parser("skip", help="Mark an event skipped without modifying Linear.")
    cli_root_arg(skip)
    skip.add_argument("--event-id", required=True)
    skip.add_argument("--issue-key")
    skip.add_argument("--reason", required=True)

    args = parser.parse_args()
    if args.command == "prepare":
        result = foreground_sync_plan(root=args.root, limit=args.limit)
    elif args.command == "ack":
        result = ack_foreground_event(
            args.event_id,
            args.issue_key,
            commit_sha=args.commit_sha,
            root=args.root,
        )
    else:
        result = skip_foreground_event(
            args.event_id,
            reason=args.reason,
            issue_key=args.issue_key,
            root=args.root,
        )
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
