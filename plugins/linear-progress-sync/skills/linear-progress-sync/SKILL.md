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

1. Use the existing Linear MCP/app connection to get or create the Linear issue.
2. Read the issue back and use Linear's generated git branch name when present.
3. Run `/linear-start` or `scripts/linear_start.py kickoff` to create/switch the branch, push an empty kickoff commit, create a draft PR, and write active state.
4. Add the PR link/comment back to Linear through Linear MCP/app tools.

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

Set up the plugin, GitHub CLI check, and Linear MCP once:

```bash
python3 plugins/linear-progress-sync/scripts/setup.py
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
