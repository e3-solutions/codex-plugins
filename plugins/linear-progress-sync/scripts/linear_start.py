#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json

from linear_sync import (
    activate_linear_start,
    cli_root_arg,
    linear_user_profile_status,
    repo_binding_status,
    run_linear_start,
    save_linear_user_profile,
    save_repo_linear_binding,
    save_repo_linear_opt_out,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the local GitHub work container for a Linear issue.")
    sub = parser.add_subparsers(dest="command", required=True)

    kickoff = sub.add_parser("kickoff", help="Create/switch branch, empty kickoff commit, draft PR, and pending state.")
    cli_root_arg(kickoff)
    kickoff.add_argument("--issue-key", required=True)
    kickoff.add_argument("--issue-title", required=True)
    kickoff.add_argument("--issue-url", required=True)
    kickoff.add_argument("--branch")
    kickoff.add_argument("--team")
    kickoff.add_argument("--project")
    kickoff.add_argument("--dry-run", action="store_true")

    activate = sub.add_parser("activate", help="Write active state after Linear PR link/comment is confirmed.")
    cli_root_arg(activate)
    activate.add_argument("--issue-key", required=True)
    activate.add_argument("--issue-title", required=True)
    activate.add_argument("--issue-url", required=True)
    activate.add_argument("--branch", required=True)
    activate.add_argument("--pr-url", required=True)
    activate.add_argument("--pr-number", required=True)
    activate.add_argument("--team")
    activate.add_argument("--project")

    binding = sub.add_parser("repo-binding", help="Print saved Linear team/project binding for this repo.")
    cli_root_arg(binding)

    configure = sub.add_parser("configure-repo", help="Save Linear team/project binding for this repo.")
    cli_root_arg(configure)
    configure.add_argument("--team")
    configure.add_argument("--project")
    configure.add_argument("--disable-linear-sync", action="store_true", help="Opt this repo out of Linear kickoff enforcement.")
    configure.add_argument("--reason", help="Optional reason when opting this repo out of Linear sync.")

    user_profile = sub.add_parser("user-profile", help="Print saved global Linear user profile.")
    cli_root_arg(user_profile)

    configure_user = sub.add_parser("configure-user", help="Save the global Linear user profile.")
    configure_user.add_argument("--linear-name", required=True)

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
    elif args.command == "activate":
        result = activate_linear_start(
            issue_key=args.issue_key,
            issue_title=args.issue_title,
            issue_url=args.issue_url,
            branch=args.branch,
            pr_url=args.pr_url,
            pr_number=args.pr_number,
            team=args.team,
            project=args.project,
            root=args.root,
        )
        print(json.dumps(result, indent=2, sort_keys=True))
    elif args.command == "repo-binding":
        print(json.dumps(repo_binding_status(root=args.root), indent=2, sort_keys=True))
    elif args.command == "configure-repo":
        if args.disable_linear_sync:
            result = save_repo_linear_opt_out(reason=args.reason, root=args.root)
        else:
            if not args.team or not args.project:
                parser.error("configure-repo requires --team and --project unless --disable-linear-sync is set")
            result = save_repo_linear_binding(team=args.team, project=args.project, root=args.root)
        print(json.dumps(result, indent=2, sort_keys=True))
    elif args.command == "user-profile":
        print(json.dumps(linear_user_profile_status(), indent=2, sort_keys=True))
    elif args.command == "configure-user":
        result = save_linear_user_profile(linear_name=args.linear_name)
        print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
