---
name: linear-progress-sync
description: Inspect, test, and safely operate the local Linear progress sync plugin that starts implementation work from a confirmed Linear issue, creates the linked branch/draft PR, and syncs Codex/Git progress without ever moving issues to terminal states.
---

# Linear Progress Sync

Use this skill when working with the local `linear-progress-sync` plugin.

## Automatic Kickoff Rule

Before writing code, creating a branch, or opening implementation changes, check for:

```bash
.codex/linear-sync/active.json
```

If it is missing, run the Linear kickoff workflow before editing:

1. Run `scripts/linear_start.py repo-binding --root <root>`.
2. If this repo has no saved team/project and no existing issue was provided, call `mcp__codex_apps__linear._list_teams` and `mcp__codex_apps__linear._list_projects`, ask the user once which Linear team/project the repo should use, then save it with `scripts/linear_start.py configure-repo`.
3. Use `mcp__codex_apps__linear._save_issue` to create the issue in the saved team/project, or `mcp__codex_apps__linear._fetch` to read an existing issue.
4. Read the issue back with `mcp__codex_apps__linear._fetch` and use Linear's generated git branch name when present.
5. Run `/linear-start` or `scripts/linear_start.py kickoff` to create/switch the branch, push an empty kickoff commit, create a draft PR, and write active state.
6. Add the PR link/comment back to Linear with `mcp__codex_apps__linear._save_issue` and `mcp__codex_apps__linear._save_comment`.

The human should not need to remember `/linear-start`; use it as the explicit/manual entrypoint when active state is missing.

## Safety Contract

- Never mark Linear issues Done, Completed, Closed, Canceled, or any terminal state.
- Only add comments and optionally move a confirmed non-terminal issue to `In Progress`.
- Do not write code or create a branch unless `.codex/linear-sync/active.json` exists, except while running the kickoff helper itself.
- Normal progress sync must use the active Linear issue first; branch/commit/fuzzy inference is legacy fallback only.
- If confidence is below `0.8`, do not write to Linear.
- If confidence is between `0.5` and `0.8`, write only to `.codex/linear-sync/review_queue.jsonl`.
- Deduplicate post-commit updates by commit SHA and issue key.
- Throttle session-progress comments to one per issue every 30 minutes.
- Use the existing Linear MCP/app connection through Codex. Do not add a direct Linear API client.

## Useful Commands

Authenticate GitHub manually before setup when needed:

```bash
gh auth login
```

Set up the plugin, GitHub CLI check, and Linear MCP once:

```bash
python3 plugins/linear-progress-sync/scripts/setup.py
```

Authenticate Linear after setup registers the MCP server when needed:

```bash
codex mcp login linear
```

Inspect or save the Linear team/project binding for a repo:

```bash
python3 plugins/linear-progress-sync/scripts/linear_start.py repo-binding --root /path/to/repo
python3 plugins/linear-progress-sync/scripts/linear_start.py configure-repo --root /path/to/repo --team "Engineering" --project "Codex Plugins"
```

Preview setup without changing Codex config:

```bash
python3 plugins/linear-progress-sync/scripts/setup.py --dry-run
```

Optionally install the local Git post-commit hook for commits made outside Codex:

```bash
python3 plugins/linear-progress-sync/scripts/install_git_hook.py
```

Dry-run the GitHub kickoff helper:

```bash
python3 plugins/linear-progress-sync/scripts/linear_start.py kickoff --issue-key COR-123 --issue-title "Implement work" --branch arya/cor-123-implement-work --dry-run
```

Dry-run the queue drain without Linear writes:

```bash
python3 plugins/linear-progress-sync/scripts/drain_queue.py --once --dry-run
```

Run the daily safety sweep:

```bash
python3 plugins/linear-progress-sync/scripts/daily_sweep.py
```


Prepare foreground sync events:

```bash
python3 plugins/linear-progress-sync/scripts/foreground_sync.py prepare --root /path/to/repo
```

Acknowledge only after the Linear comment is visibly confirmed:

```bash
python3 plugins/linear-progress-sync/scripts/foreground_sync.py ack --root /path/to/repo --event-id <id> --issue-key <KEY>
```

Inspect local state:

```bash
find .codex/linear-sync -maxdepth 3 -type f -print
```
