# Linear Progress Sync

`linear-progress-sync` is a local Codex plugin that queues real development progress from Codex hooks and Git commits, then asks Codex to update inferred Linear issues through the existing Linear MCP/app connection.

It is intentionally conservative:

- It never marks issues Done, Completed, Closed, Canceled, or any terminal state.
- It only adds comments and optionally moves confirmed non-terminal issues to `In Progress`.
- It deduplicates commit updates by commit SHA and issue key.
- It throttles long-session progress comments to one per issue every 30 minutes.
- It skips Linear writes when issue inference confidence is low.

## Install In Codex App

This repo includes a repo-local marketplace at:

```bash
.agents/plugins/marketplace.json
```

If the marketplace is not already configured in Codex, add the repo marketplace root:

```bash
codex plugin marketplace add /path/to/codex-plugins
codex plugin add linear-progress-sync@coreedge-local
```

Start a new Codex thread after installing so the hooks and skill are loaded.

## Trust Hooks

Review these hook files before enabling the plugin:

```bash
plugins/linear-progress-sync/hooks.json
plugins/linear-progress-sync/hooks/hooks.json
```

The hooks only enqueue local events and start the queue worker in the background. Linear writes happen only through `codex exec --ephemeral` from `drain_queue.py`.

## Install Git Post-Commit Hook

Run this from the repo root:

```bash
python3 plugins/linear-progress-sync/scripts/install_git_hook.py
```

If an existing `.git/hooks/post-commit` hook exists, the installer backs it up before writing the Linear sync hook. Use `--force` only if you intentionally want to replace the existing hook without a backup.


## Foreground Sync Command

If background sync cannot complete Linear writes because MCP approval is hidden or cancelled, process queued events in the active Codex thread:

```text
/sync-linear-progress root=/path/to/target-repo
```

The command prepares eligible queued events, asks Codex to use visible Linear MCP calls, and only acknowledges a queued event after the Linear comment is visible on read-back.

You can inspect the same plan manually:

```bash
python3 /path/to/codex-plugins/plugins/linear-progress-sync/scripts/foreground_sync.py prepare --root /path/to/target-repo
```

After a comment is confirmed visible, the command runs the generated `ack_command`. If approval is denied or the comment is not visible, the event remains queued.

## Test With One Commit

1. Make a small commit on a branch that contains a Linear issue key, for example `nitish/cor-2341-test-sync`.
2. The Git hook enqueues a `post_commit` event.
3. Dry-run the queue drain:

```bash
python3 plugins/linear-progress-sync/scripts/drain_queue.py --once --dry-run
```

4. Inspect the local review queue/state:

```bash
find .codex/linear-sync -maxdepth 3 -type f -print
cat .codex/linear-sync/state.json
cat .codex/linear-sync/review_queue.jsonl
```

To attempt a real Linear update, run:

```bash
python3 plugins/linear-progress-sync/scripts/drain_queue.py --once
```

This requires the Codex CLI and Linear MCP/app auth to be available.

## Enable Daily Sweep

Run manually:

```bash
python3 plugins/linear-progress-sync/scripts/daily_sweep.py
```

To automate locally, add that command to your preferred cron/launchd scheduler from the repo root.

## Enable GitHub Actions

The workflow lives at:

```bash
.github/workflows/linear-sync.yml
```

It runs on pushes to `main` and `dev`, enqueues pushed commits, and drains the queue. It fails safely if Codex or Linear auth is not available in CI.

## Inspect Local State

State is stored under:

```bash
.codex/linear-sync/
```

Important files:

- `events/`: queued event JSON files.
- `state.json`: processed event IDs, synced commit SHAs, throttling timestamps, cached issue context, and failure counts.
- `review_queue.jsonl`: medium-confidence or dry-run events for manual review.
- `logs/`: reserved for local worker logs.

## Disable Sync

Disable the Codex plugin from the Codex app, or remove the Git hook:

```bash
rm .git/hooks/post-commit
```

Queued events are local files. Remove them if you want a clean slate:

```bash
rm -rf .codex/linear-sync
```
