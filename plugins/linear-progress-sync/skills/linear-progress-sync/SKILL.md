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

Read-only inspection is allowed before kickoff. Before the first write or branch creation in any repo, Codex must have a saved Linear team/project binding or an existing active state.

Pre-kickoff Bash uses a read-only allowlist, not a write-command blacklist. Allow simple inspection commands such as `pwd`, `ls`, `cat`, `head`, `tail`, `wc`, `rg`, `grep`, `sed` without `-i`, `find` without exec/delete actions, `stat`, `date`, and read-only `git` commands. Block unknown shell commands until active Linear state exists.

If active state is missing, run the Linear kickoff workflow before editing:

1. Run `scripts/linear_start.py user-profile --root <root>`.
2. If there is no saved user profile, ask them what their name on Linear is, then save it once with `scripts/linear_start.py configure-user --linear-name "<Linear user name>"`. This is global for every repo.
3. Run `scripts/linear_start.py repo-binding --root <root>`.
4. If this repo has no saved team/project, call `mcp__codex_apps__linear._list_teams` and `mcp__codex_apps__linear._list_projects`, ask the user once which Linear team/project the repo should use, then save it with `scripts/linear_start.py configure-repo`.
   - If the direct Linear MCP namespace is exposed instead, use `mcp__linear.list_teams` and `mcp__linear.list_projects`.
   - If the session exposes short Linear aliases like `list_teams` or `list_projects`, use those aliases. If create/update tools are not visible after listing, search/load Linear tools; do not stop after listing projects. Issue creation/linking tools are `mcp__codex_apps__linear._save_issue`/`mcp__codex_apps__linear._save_comment` or `mcp__linear.save_issue`/`mcp__linear.save_comment`.
   - Do not create the Linear issue, branch, PR, or code changes until the chosen repo destination is saved.
   - If the write guard blocks because no repo destination is saved, do not answer with a code patch or say you are blocked. Continue by listing Linear destinations and asking the user which team/project this repo should use.
5. Unless the user explicitly supplied an existing Linear issue key, create a new issue automatically from the user's implementation request in the saved team/project and assign it to the saved Linear user name. Do not ask the user for a Linear issue key.
6. Use `mcp__codex_apps__linear._save_issue` to create the issue in the saved team/project, or `mcp__codex_apps__linear._get_issue`/`mcp__codex_apps__linear._fetch` to read an explicitly supplied existing issue.
   - Direct Linear MCP equivalents are `mcp__linear.save_issue` and `mcp__linear.get_issue`.
7. Read the issue back with `mcp__codex_apps__linear._get_issue`/`mcp__codex_apps__linear._fetch` or `mcp__linear.get_issue` and use Linear's generated git branch name when present.
8. Run `/linear-start` or `scripts/linear_start.py kickoff` to create/switch the branch, push an empty kickoff commit, and create a draft PR. Treat the returned `pending_active_state` as inactive until Linear linking is confirmed.
   - If a write is blocked after the Linear issue was created, do not stop, do not test write access, and do not ask the user to activate state. Reuse the issue key, URL, title, and Linear `gitBranchName`; run the kickoff helper; link Linear and GitHub; then run the helper's `activation_command`.
9. Add the PR link/comment back to Linear with `mcp__codex_apps__linear._save_issue` and `mcp__codex_apps__linear._save_comment`.
   - Direct Linear MCP equivalents are `mcp__linear.save_issue` and `mcp__linear.save_comment`.
   - End every Linear issue body and comment Codex creates with `Codex bot: <stored Linear user name> at <ISO-8601 UTC timestamp>`.
10. Read Linear back or otherwise confirm the PR link/comment is visible, then run `scripts/linear_start.py activate` with the helper's `activation_command` to write `.codex/linear-sync/active.json`.

The human should not need to remember `/linear-start`; use it as the explicit/manual entrypoint when active state is missing.

## Safety Contract

- Never mark Linear issues Done, Completed, Closed, Canceled, or any terminal state.
- Only add comments and optionally move a confirmed non-terminal issue to `In Progress`.
- In Linear-sync repos, do not write code or create a branch unless `.codex/linear-sync/active.json` exists, except while running the kickoff helper itself.
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

If Codex asks to review hooks after setup, trust the Linear Progress Sync hooks once. Automatic kickoff depends on those hooks running.

Authenticate Linear after setup registers the MCP server when needed:

```bash
codex mcp login linear
```

Inspect or save the Linear team/project binding for a repo:

```bash
python3 plugins/linear-progress-sync/scripts/linear_start.py repo-binding --root /path/to/repo
python3 plugins/linear-progress-sync/scripts/linear_start.py user-profile --root /path/to/repo
python3 plugins/linear-progress-sync/scripts/linear_start.py configure-user --linear-name "Your Linear Name"
python3 plugins/linear-progress-sync/scripts/linear_start.py configure-repo --root /path/to/repo --team "Engineering" --project "Codex Plugins"
```

Preview setup without changing Codex config:

```bash
python3 plugins/linear-progress-sync/scripts/setup.py --dry-run
```

Optionally install the local Git post-commit hook for commits made outside Codex:

```bash
python3 plugins/linear-progress-sync/scripts/setup.py --with-git-hook --root /path/to/repo
```

Dry-run the GitHub kickoff helper:

```bash
python3 plugins/linear-progress-sync/scripts/linear_start.py kickoff --issue-key COR-123 --issue-title "Implement work" --issue-url "https://linear.app/example/issue/COR-123/implement-work" --branch arya/cor-123-implement-work --dry-run
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
