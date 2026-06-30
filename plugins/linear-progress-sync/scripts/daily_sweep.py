#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from linear_sync import cli_root_arg, collect_today_commits, drain_once, enqueue_event, read_state


def main() -> None:
    parser = argparse.ArgumentParser(description="Enqueue today's missing commit events and drain once.")
    parser.add_argument("--dry-run", action="store_true", help="Do not write to Linear; record decisions locally.")
    cli_root_arg(parser)
    args = parser.parse_args()

    state = read_state(args.root)
    queued = []
    for commit in collect_today_commits(root=args.root):
        sha = commit.get("commit_sha")
        if sha in set(state.get("synced_commit_shas") or []):
            continue
        event = enqueue_event("post_commit", {**commit, "source": "daily_sweep"}, root=args.root)
        queued.append(event["id"])
    result = drain_once(root=args.root, dry_run=args.dry_run)
    print(json.dumps({"queued": queued, "drain": result}, sort_keys=True))


if __name__ == "__main__":
    main()

