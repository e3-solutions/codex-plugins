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

Pre-kickoff Bash blocks write-like commands and branch creation, not every unknown command. Allow read-only and non-mutating commands before kickoff. Block file edits, commands that appear to write files or mutate repo state, and branch creation until active Linear state exists.

Linear kickoff enforcement only applies to repos whose `origin` remote is under the `e3-solutions` GitHub org. Repos with no `origin` remote or another GitHub org are out of scope and should be allowed without Linear kickoff.

If active state is missing, run the Linear kickoff workflow before editing:

1. Run `scripts/linear_start.py user-profile --root <root>`.
2. If there is no saved user profile, call `mcp__codex_apps__linear._list_users` or `mcp__linear.list_users`, present the active human users, ask them to choose their Linear user from that list, then save the selected Linear `name` once with `scripts/linear_start.py configure-user --linear-name "<Linear user name>"`. This is global for every repo.
3. Run `scripts/linear_start.py repo-binding --root <root>`.
4. If this repo has no saved team/project, call `mcp__codex_apps__linear._list_teams` and `mcp__codex_apps__linear._list_projects`, present the Linear project list, ask the user once to choose the project from that list, then save it with `scripts/linear_start.py configure-repo`.
   - If the direct Linear MCP namespace is exposed instead, use `mcp__linear.list_teams` and `mcp__linear.list_projects`.
   - If the session exposes short Linear aliases like `list_teams` or `list_projects`, use those aliases. If create/update tools are not visible after listing, search/load Linear tools; do not stop after listing projects. Issue creation/linking tools are `mcp__codex_apps__linear._save_issue`/`mcp__codex_apps__linear._save_comment` or `mcp__linear.save_issue`/`mcp__linear.save_comment`.
   - Do not create the Linear issue, branch, PR, or code changes until the chosen repo destination is saved.
   - If the user says this repo should not use Linear sync, save the opt-out with `scripts/linear_start.py configure-repo --root <root> --disable-linear-sync --reason "<reason>"`; future work in that repo is allowed without Linear kickoff until a normal team/project binding is saved again.
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
- Only post-commit events may create Linear progress comments.
- Deduplicate post-commit updates by commit SHA and issue key.
- Use the existing Linear MCP/app connection through Codex. Do not add a direct Linear API client.

## Useful Commands

Authenticate GitHub manually before setup when needed:

```bash
gh auth login
```

Set up the plugin, GitHub CLI check, and Linear MCP once:

```bash
git clone https://github.com/e3-solutions/codex-plugins
cd codex-plugins
gh auth login
python3 plugins/linear-progress-sync/scripts/setup.py
codex mcp login linear
```

This repository is a Codex plugin marketplace, not a single plugin source. Do not tell teammates or agents to run `codex plugin add` with the GitHub URL or repository root directly; that can skip marketplace registration, default plugin installation, legacy hook cleanup, and Linear MCP registration.

Setup installs a resident updater under `~/.codex/coreedge`. It checks at login and every 30 minutes through a macOS LaunchAgent or Linux systemd user timer, validates and stages the current `main.zip`, atomically switches the managed marketplace, selects one cache version per default plugin, and retains rollback copies outside Codex's cache scan. Linux VMs use user units under `$XDG_CONFIG_HOME/systemd/user` when that variable is set and `~/.config/systemd/user` otherwise. Headless VMs need `loginctl enable-linger <user>` if the timers must continue after logout. SessionStart and PreToolUse self-heal a missing service. Persistently disable or re-enable network checks with:

Existing installations activate `0.3.4` during one ordinary resident check and self-heal without rerunning setup. The release preserves historical-backfill protections, Linux systemd support, and commit-only Linear progress comments while extending the metadata-only one-minute task-presence publisher to subagents with parent-thread linkage. Presence does not depend on an already-running Codex process reloading hooks. Do not ask teammates to run an update command, deliberately restart Codex, or create a renewal thread for normal updates. Fresh setup installs and schedules the current plugins immediately.

```bash
python3 ~/.codex/coreedge/runtime/current/update_plugin.py --disable-auto-update
python3 ~/.codex/coreedge/runtime/current/update_plugin.py --enable-auto-update
```

`LINEAR_SYNC_AUTO_UPDATE=0` is also honored and is persisted when setup or a self-healing hook observes it.

Force a manual update check when needed:

```bash
python3 ~/.codex/coreedge/runtime/current/update_plugin.py --force
```

Inspect updater health:

```bash
python3 ~/.codex/coreedge/runtime/current/update_plugin.py --doctor
```

The doctor result includes both the resident updater and the metadata-only task-presence LaunchAgent or systemd user timer. Presence runs once per minute and does not require Codex to reload hooks.

If Codex asks to review hooks after setup, trust the Linear Progress Sync and Codex Session Logging hooks once. Automatic kickoff and session capture depend on those hooks running.

To roll out a new default plugin, skill, command, hook, or extension after teammates have run setup once:

1. Add or update the plugin under `plugins/<name>/`.
2. Set a new version in `plugins/<name>/.codex-plugin/plugin.json`.
3. Add the plugin to `.agents/plugins/marketplace.json`.
4. Set its marketplace policy to `INSTALLED_BY_DEFAULT`.
5. Bump `plugins/linear-progress-sync/.codex-plugin/plugin.json` and `plugins/linear-progress-sync/update-manifest.json`.
6. Merge to `main`.

Do not tell teammates to reinstall, run an update command, or create a renewal thread for normal default plugin updates. The resident service activates them; a later normal task naturally reloads changed skill text.

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
python3 plugins/linear-progress-sync/scripts/linear_start.py configure-repo --root /path/to/repo --disable-linear-sync --reason "No Linear tracking"
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
