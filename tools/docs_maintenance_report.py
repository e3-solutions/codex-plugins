#!/usr/bin/env python3
"""Weekly evidence that repo docs are being maintained (COR-5202, COR-5207).

For each e3-solutions repository that has the agent docs system (a
`.agents/check_agent_docs.py` on its default branch), counts over the window:

  merged        merged PRs
  code          merged PRs that changed code
  code+docs     PRs that changed code and agent docs together
                  (AGENTS.md, CLAUDE.md, docs/, .agents/, .claude/, .codex/, any *.md)
  dc-updated    PRs with a `Docs-Check: updated` commit trailer
  dc-none       PRs with a `Docs-Check: none-needed` trailer
  stale-risk    PRs the docs backstop would act on: code changed (10+ lines or a
                  code file added/removed), no docs, no Docs-Check trailer.
                  Same rules as .agents/docs_backstop_gate.py, imported from it
  doc-commits   commits on the default branch that changed AGENTS.md, CLAUDE.md,
                  docs/ or .agents/skills/
  by-agent      of those, commits written with a coding agent (a Claude or Codex
                  co-author or "Generated with" line, or the Actions bot)
  backstop      docs backstop runs that found code without docs (the gate said
                  needed=true), from the docs-backstop.yml workflow
  bs-prs        docs backstop PRs opened (head branch docs-backstop/...), merged in ()
  drift         problems on the latest completed Agent docs run on the default
                  branch (its "agent docs check: N problem(s)" log line): paths,
                  commands or skills the docs name that no longer exist

Repos with merged PRs but no docs system are listed separately, with their PR count.
Read-only; uses the GitHub CLI (`gh`) with the caller's own login.

Usage: python3 tools/docs_maintenance_report.py [--days 7] [--org e3-solutions]
           [--repos a,b] [--json] [--markdown] [--out PATH]
"""
from __future__ import annotations

import argparse
import datetime as dt
import importlib.util
import json
import os
import re
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

_GATE = Path(__file__).resolve().parents[1] / ".agents" / "docs_backstop_gate.py"
_spec = importlib.util.spec_from_file_location("docs_backstop_gate", _GATE)
gate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gate)
AGENT_DOC_PATHS = ("AGENTS.md", "CLAUDE.md", "docs", ".agents/skills")
AGENT_MARK = re.compile(r"co-authored-by:.*(claude|codex|openai)|generated with \[?(claude|codex)", re.I)
BOT_LOGINS = {"github-actions[bot]", "claude[bot]"}
# Repos that got the docs system; others are found from merged PRs.
KNOWN_REPOS = (
    "sherlock", "sesh", "jolly-roger", "e3-mcp", "codex-plugins", "negotiation", "kch-quoter",
    "meetius", "rollouts", "heartbeat-dashboard", "e3-heartbeat", "e3-email-mcp",
    "e3-google-chat-mcp", "e3-github-mcp", "e3-salesforce-mcp", "dev-metrics",
)


def gh(*args: str) -> object:
    out = subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout
    return json.loads(out) if out.strip() else None


def gh_lines(*args: str) -> list[str]:
    out = subprocess.run(["gh", *args], capture_output=True, text=True, check=True).stdout
    return [line for line in out.splitlines() if line]


def gh_ok(*args: str) -> bool:
    return subprocess.run(["gh", *args], capture_output=True, text=True).returncode == 0


def search(query: str) -> list[dict]:
    items, page = [], 1
    while True:
        result = gh("api", "-X", "GET", "search/issues", "-f", f"q={query}", "-f", "per_page=100", "-f", f"page={page}")
        items += result["items"]
        if len(result["items"]) < 100 or page >= 10:
            return items
        page += 1


def pr_facts(repo: str, number: int) -> dict:
    changes, messages = gate.from_github(repo, str(number))
    stale, _reason, code = gate.decide(changes, messages)
    trailer = None
    for line in messages.splitlines():
        if line.lower().startswith("docs-check:"):
            trailer = line.split(":", 1)[1].strip().lower()
    return {"code": bool(code), "docs": any(gate.is_doc(c["filename"]) for c in changes),
            "trailer": trailer, "stale": stale}


def doc_commits(repo: str, since: str) -> tuple[int, int]:
    seen: dict[str, bool] = {}
    for path in AGENT_DOC_PATHS:
        rows = gh_lines("api", "-X", "GET", f"repos/{repo}/commits", "-f", f"since={since}T00:00:00Z", "-f", f"path={path}",
                        "-f", "per_page=100", "--paginate", "--jq", '.[] | {sha, m: .commit.message, a: (.author.login // "")} | @json')
        for row in rows:
            c = json.loads(row)
            seen[c["sha"]] = bool(AGENT_MARK.search(c["m"])) or c["a"] in BOT_LOGINS
    return len(seen), sum(seen.values())


def backstop_runs(repo: str, since: str) -> int:
    """Runs of docs-backstop.yml whose gate found code without docs."""
    if not gh_ok("api", f"repos/{repo}/actions/workflows/docs-backstop.yml"):
        return 0
    runs = gh_lines("api", "-X", "GET", f"repos/{repo}/actions/workflows/docs-backstop.yml/runs",
                    "-f", f"created=>={since}", "-f", "per_page=100", "--paginate", "--jq", ".workflow_runs[].id")
    needed = 0
    for run in runs:
        steps = gh_lines("api", f"repos/{repo}/actions/runs/{run}/jobs", "--jq",
                         '.jobs[].steps[] | select(.name == "Update docs with Claude") | .conclusion')
        needed += any(s in ("success", "failure") for s in steps)
    return needed


PROBLEMS = re.compile(r"agent docs check: (\d+) problem")


def drift(repo: str, branch: str) -> int | None:
    """Problems on the latest completed Agent docs run on the default branch.

    Read from the job log, not annotations: GitHub keeps at most 10 warnings per step."""
    if not gh_ok("api", f"repos/{repo}/actions/workflows/agent-docs.yml"):
        return None
    runs = gh_lines("api", "-X", "GET", f"repos/{repo}/actions/workflows/agent-docs.yml/runs",
                    "-f", f"branch={branch}", "-f", "status=completed", "-f", "per_page=1", "--jq", ".workflow_runs[].id")
    if not runs:
        return None
    found = None
    for job in gh_lines("api", f"repos/{repo}/actions/runs/{runs[0]}/jobs", "--jq", ".jobs[].id"):
        log = subprocess.run(["gh", "api", f"repos/{repo}/actions/jobs/{job}/logs"], capture_output=True, text=True).stdout
        for match in PROBLEMS.finditer(log):
            found = (found or 0) + int(match.group(1))
    return found


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--days", type=int, default=7)
    parser.add_argument("--org", default="e3-solutions")
    parser.add_argument("--repos", help="comma-separated repo names (default: known docs repos + repos with merged PRs)")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--markdown", action="store_true")
    parser.add_argument("--out", help="write the report to this file instead of stdout")
    args = parser.parse_args()
    since = (dt.date.today() - dt.timedelta(days=args.days)).isoformat()
    if subprocess.run(["gh", "auth", "status"], capture_output=True).returncode != 0:
        hint = " GITHUB_TOKEN is set and may be invalid: try `env -u GITHUB_TOKEN`." if os.environ.get("GITHUB_TOKEN") else ""
        sys.exit("gh is not logged in (gh auth status failed)." + hint)

    merged: dict[str, list[int]] = defaultdict(list)
    for item in search(f"org:{args.org} is:pr is:merged merged:>={since}"):
        merged[item["repository_url"].split("/repos/", 1)[1]].append(item["number"])
    names = args.repos.split(",") if args.repos else sorted(set(KNOWN_REPOS) | {r.split("/")[1] for r in merged})
    bs_opened: dict[str, int] = defaultdict(int)
    bs_merged: dict[str, int] = defaultdict(int)
    for item in search(f"org:{args.org} is:pr head:docs-backstop/ created:>={since}"):
        repo = item["repository_url"].split("/repos/", 1)[1]
        # Search matches head:docs-backstop as a word prefix; keep only real backstop branches.
        head = gh_lines("api", f"repos/{repo}/pulls/{item['number']}", "--jq", ".head.ref")
        if not head or not head[0].startswith("docs-backstop/"):
            continue
        bs_opened[repo] += 1
        bs_merged[repo] += bool((item.get("pull_request") or {}).get("merged_at"))

    rows: dict[str, dict[str, object]] = {}
    uncovered: dict[str, int] = {}
    for name in names:
        repo = f"{args.org}/{name}"
        info = gh("api", f"repos/{repo}") if gh_ok("api", f"repos/{repo}") else None
        if not info or info.get("archived"):
            continue
        branch = info["default_branch"]
        if not gh_ok("api", f"repos/{repo}/contents/.agents/check_agent_docs.py?ref={branch}"):
            if merged.get(repo):
                uncovered[name] = len(merged[repo])
            continue
        facts = [pr_facts(repo, n) for n in merged.get(repo, [])]
        code = sum(f["code"] for f in facts)
        commits, by_agent = doc_commits(repo, since)
        rows[name] = {
            "merged": len(facts), "code": code,
            "code+docs": sum(f["code"] and f["docs"] for f in facts),
            "dc-updated": sum(f["trailer"] == "updated" for f in facts),
            "dc-none": sum(f["trailer"] == "none-needed" for f in facts),
            "stale-risk": sum(f["stale"] for f in facts),
            "doc-commits": commits, "by-agent": by_agent,
            "backstop": backstop_runs(repo, since),
            "bs-prs": f"{bs_opened[repo]} ({bs_merged[repo]})",
            "drift": drift(repo, branch),
        }

    if args.json:
        text = json.dumps({"since": since, "repos": rows, "uncovered_merged_prs": uncovered}, indent=2, sort_keys=True)
    else:
        columns = ("merged", "code", "code+docs", "dc-updated", "dc-none", "stale-risk",
                   "doc-commits", "by-agent", "backstop", "bs-prs", "drift")
        order = sorted(rows, key=lambda r: (-int(rows[r]["merged"]), r))
        cell = lambda v: "n/a" if v is None else str(v)
        if args.markdown:
            lines = [f"| repo | {' | '.join(columns)} |", "|---" * (len(columns) + 1) + "|"]
            lines += [f"| {r} | " + " | ".join(cell(rows[r][c]) for c in columns) + " |" for r in order]
            total = lambda c: sum(int(rows[r][c] or 0) for r in rows if isinstance(rows[r][c], int) or rows[r][c] is None)
            lines.append("| **total** | " + " | ".join(
                f"{sum(bs_opened[f'{args.org}/{r}'] for r in rows)} ({sum(bs_merged[f'{args.org}/{r}'] for r in rows)})"
                if c == "bs-prs" else str(total(c)) for c in columns) + " |")
            if uncovered:
                lines += ["", "Repos with merged PRs but no docs system: "
                          + ", ".join(f"{r} ({n})" for r, n in sorted(uncovered.items(), key=lambda x: -x[1]))]
        else:
            lines = [f"{'repo':24} " + " ".join(f"{c:>11}" for c in columns)]
            lines += [f"{r:24} " + " ".join(f"{cell(rows[r][c]):>11}" for c in columns) for r in order]
            if uncovered:
                lines.append("no docs system, merged PRs: " + ", ".join(f"{r}={n}" for r, n in sorted(uncovered.items())))
        text = f"Docs maintenance since {since} ({args.days} days)\n\n" + "\n".join(lines)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    else:
        print(text)


if __name__ == "__main__":
    main()
