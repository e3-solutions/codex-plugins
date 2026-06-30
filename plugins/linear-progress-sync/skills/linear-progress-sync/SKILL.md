---
name: linear-progress-sync
description: Inspect, test, and safely operate the local Linear progress sync plugin that queues Codex/Git progress events and asks Codex to comment on inferred Linear issues without ever moving issues to terminal states.
---

# Linear Progress Sync

Use this skill when working with the local `linear-progress-sync` plugin.

## Safety Contract

- Never mark Linear issues Done, Completed, Closed, Canceled, or any terminal state.
- Only add comments and optionally move a confirmed non-terminal issue to `In Progress`.
- If confidence is below `0.8`, do not write to Linear.
- If confidence is between `0.5` and `0.8`, write only to `.codex/linear-sync/review_queue.jsonl`.
- Deduplicate post-commit updates by commit SHA and issue key.
- Throttle session-progress comments to one per issue every 30 minutes.
- Use the existing Linear MCP/app connection through Codex. Do not add a direct Linear API client.

## Useful Commands

Install the local Git post-commit hook:

```bash
python3 plugins/linear-progress-sync/scripts/install_git_hook.py
```

Dry-run the queue drain without Linear writes:

```bash
python3 plugins/linear-progress-sync/scripts/drain_queue.py --once --dry-run
```

Run the daily safety sweep:

```bash
python3 plugins/linear-progress-sync/scripts/daily_sweep.py
```

Inspect local state:

```bash
find .codex/linear-sync -maxdepth 3 -type f -print
```

