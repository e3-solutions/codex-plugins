#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from linear_sync import cli_root_arg, repo_binding_status, run_linear_start, save_repo_linear_binding


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the local GitHub work container for a Linear issue.")
    sub = parser.add_subparsers(dest="command", required=True)

    kickoff = sub.add_parser("kickoff", help="Create/switch branch, empty kickoff commit, draft PR, and active state.")
    cli_root_arg(kickoff)
    kickoff.add_argument("--issue-key", required=True)
    kickoff.add_argument("--issue-title", required=True)
    kickoff.add_argument("--issue-url")
    kickoff.add_argument("--branch")
    kickoff.add_argument("--team")
    kickoff.add_argument("--project")
    kickoff.add_argument("--dry-run", action="store_true")

    binding = sub.add_parser("repo-binding", help="Print saved Linear team/project binding for this repo.")
    cli_root_arg(binding)

    configure = sub.add_parser("configure-repo", help="Save Linear team/project binding for this repo.")
    cli_root_arg(configure)
    configure.add_argument("--team", required=True)
    configure.add_argument("--project", required=True)

    args = parser.parse_args()
    if args.command == "kickoff":
        result = run_linear_start(
            issue_key=args.issue_key,
            issue_title=args.issue_title,
            issue_url=args.issue_url,
            branch=args.branch,
            team=args.team,
            project=args.project,
            root=args.root,
            dry_run=args.dry_run,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
    elif args.command == "repo-binding":
        print(json.dumps(repo_binding_status(root=args.root), indent=2, sort_keys=True))
    elif args.command == "configure-repo":
        result = save_repo_linear_binding(team=args.team, project=args.project, root=args.root)
        print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
