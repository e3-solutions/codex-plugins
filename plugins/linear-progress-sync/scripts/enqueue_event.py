#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from linear_sync import cli_root_arg, collect_commit_event, enqueue_event


def main() -> None:
    parser = argparse.ArgumentParser(description="Enqueue a Linear progress sync event.")
    parser.add_argument("event_type", nargs="?", default="manual")
    parser.add_argument("--from-git", action="store_true", help="Collect the latest commit as a post_commit event.")
    parser.add_argument("--sha", help="Specific commit SHA to collect.")
    parser.add_argument("--json", dest="json_payload", help="Additional event payload as JSON.")
    cli_root_arg(parser)
    args = parser.parse_args()

    payload = {}
    if args.json_payload:
        loaded = json.loads(args.json_payload)
        if not isinstance(loaded, dict):
            raise SystemExit("--json must contain a JSON object")
        payload.update(loaded)
    if args.from_git or args.event_type == "post_commit":
        payload.update(collect_commit_event(args.sha, root=args.root))
    event = enqueue_event(args.event_type, payload, root=args.root)
    print(json.dumps({"queued": event["id"], "type": event["type"]}))


if __name__ == "__main__":
    main()

