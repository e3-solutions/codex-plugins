#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from linear_sync import cli_root_arg, run_linear_start


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the local GitHub work container for a Linear issue.")
    sub = parser.add_subparsers(dest="command", required=True)

    kickoff = sub.add_parser("kickoff", help="Create/switch branch, empty kickoff commit, draft PR, and active state.")
    cli_root_arg(kickoff)
    kickoff.add_argument("--issue-key", required=True)
    kickoff.add_argument("--issue-title", required=True)
    kickoff.add_argument("--issue-url")
    kickoff.add_argument("--branch")
    kickoff.add_argument("--dry-run", action="store_true")

    args = parser.parse_args()
    if args.command == "kickoff":
        result = run_linear_start(
            issue_key=args.issue_key,
            issue_title=args.issue_title,
            issue_url=args.issue_url,
            branch=args.branch,
            root=args.root,
            dry_run=args.dry_run,
        )
        print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
