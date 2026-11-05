#!/usr/bin/env python3
"""Weekly evidence that repo docs are being maintained (COR-5202).

For each e3-solutions repository with merged PRs in the window, counts:
  merged       merged PRs
  code         PRs that changed code
  code+docs    PRs that changed code and agent docs together
                 (AGENTS.md, CLAUDE.md, docs/, .agents/, any *.md)
  checked      PRs with a `Docs-Check:` commit trailer, which the docs-maintenance
                 Stop hook asks agents to add (updated / none-needed)
  stale-risk   PRs that changed code but no docs and carry no Docs-Check trailer

Read-only; uses the GitHub CLI (`gh`) with the caller's own login.

Usage: python3 tools/docs_maintenance_report.py [--days 7] [--org e3-solutions] [--json]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
from collections import defaultdict

DOC_PREFIXES = ("docs/", ".agents/", ".claude/", ".codex/")


def gh(*args: str) -> object:
    out = subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout
    return json.loads(out) if out.strip() else None


def gh_lines(*args: str) -> list[str]:
    out = subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line]


def is_doc(path: str) -> bool:
    return path.endswith(".md") or path.startswith(DOC_PREFIXES)


def is_code(path: str) -> bool:
    return not is_doc(path) and not path.endswith((".lock", "-lock.json", "-lock.yaml"))


def merged_prs(org: str, since: str) -> list[dict]:
    items, page = [], 1
    while True:
        result = gh("api", "-X", "GET", "search/issues", "-f", f"q=org:{org} is:pr is:merged merged:>={since}",
                    "-f", "per_page=100", "-f", f"page={page}")
        items += result["items"]
        if len(result["items"]) < 100 or page >= 10:
            return items
        page += 1


def pr_facts(repo: str, number: int) -> dict:
    files = gh_lines("api", f"repos/{repo}/pulls/{number}/files", "--paginate", "--jq", ".[].filename")
    commits = gh_lines("api", f"repos/{repo}/pulls/{number}/commits", "--paginate", "--jq", ".[].commit.message | @json")
    commits = [json.loads(c) for c in commits]
    trailer = None
    for message in commits:
        for line in message.splitlines():
            if line.lower().startswith("docs-check:"):
                trailer = line.split(":", 1)[1].strip().lower()
    return {"code": any(is_code(f) for f in files), "docs": any(is_doc(f) for f in files), "trailer": trailer}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--org", default="e3-solutions")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    since = (dt.date.today() - dt.timedelta(days=args.days)).isoformat()
    rows: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for item in merged_prs(args.org, since):
        repo = item["repository_url"].split("/repos/", 1)[1]
        facts = pr_facts(repo, item["number"])
        row = rows[repo]
        row["merged"] += 1
        row["code"] += facts["code"]
        row["code+docs"] += facts["code"] and facts["docs"]
        row["checked"] += facts["trailer"] is not None
        row["updated"] += facts["trailer"] == "updated"
        row["stale-risk"] += facts["code"] and not facts["docs"] and facts["trailer"] is None
    if args.json:
        print(json.dumps({"since": since, "repos": rows}, indent=2, sort_keys=True))
        return
    columns = ("merged", "code", "code+docs", "checked", "updated", "stale-risk")
    print(f"Docs maintenance, merged PRs since {since}")
    print(f"{'repo':34} " + " ".join(f"{c:>10}" for c in columns))
    for repo in sorted(rows, key=lambda r: -rows[r]["merged"]):
        print(f"{repo.split('/')[-1]:34} " + " ".join(f"{rows[repo][c]:>10}" for c in columns))


if __name__ == "__main__":
    main()
