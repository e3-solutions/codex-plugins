# Linear Progress Sync

Local Codex plugin for Linear-first development in `e3-solutions` repos. Codex starts work from a Linear issue, creates the linked branch and draft PR before code, then keeps Linear updated from Codex commits.

## Agent Install Contract

This repository is a Codex plugin marketplace, not a single plugin source. Do not install the GitHub URL or repository root directly with `codex plugin add`.

Agents installing this for a teammate must clone the repo and run `setup.py`, because setup registers the marketplace, installs both required plugins, removes legacy duplicate global hooks, and registers Linear MCP.

Current behavior:

- Enforcement is scoped to git repos whose `origin` remote is under `e3-solutions/*`.
- Repos with no `origin` remote, no git repo, or a non-E3 origin are out of scope and file edits are allowed without Linear kickoff.
- Before kickoff in scoped repos, only file edits, write-like Bash commands, and branch creation are blocked. Read-only inspection and non-mutating commands are allowed.
- Installed plugin caches auto-check for updates on every Codex `SessionStart`, install newer plugin versions from the current `main` archive, sync default marketplace plugins, and remove legacy duplicate global hook registrations.

## Setup

Run this once per teammate, not once per repo:

```bash
git clone https://github.com/e3-solutions/codex-plugins
cd codex-plugins
gh auth login
python3 plugins/linear-progress-sync/scripts/setup.py
codex mcp login linear
```

Then restart Codex or start a new Codex thread. If Codex asks to review hooks, trust the Linear Progress Sync and Codex Session Logging hooks once.

`setup.py` checks GitHub CLI auth, installs Linear Progress Sync and Codex Session Logging, removes legacy Core Edge hook copies from `~/.codex/hooks.json`, and registers Linear MCP. It does not log you in to Linear, so `codex mcp login linear` is still required.

Preview setup without changing Codex config:

```bash
python3 plugins/linear-progress-sync/scripts/setup.py --dry-run
```

## Normal Use

Start a coding task normally. Before the first edit or branch creation, Codex must create or confirm the Linear issue, create the Linear-named branch, push an empty kickoff commit, open a draft PR, link Linear and GitHub, and write local active state.

Linear kickoff enforcement only applies to repos whose `origin` remote is under the `e3-solutions` GitHub org. Repos with no `origin` remote or another GitHub org are treated as out of scope and are allowed without Linear kickoff.

The first time the plugin is used, Codex lists Linear workspace users, asks you to choose your Linear user from that list, and saves the selected name in `~/.codex/linear-sync/user.json`. Future repos reuse that profile to assign newly created Linear issues and to add deterministic attribution to Linear issue bodies and comments.

The first time a repo needs a new Linear issue, Codex lists Linear teams/projects, asks you to choose the project from that list, and saves it in `~/.codex/linear-sync/repos.json`. Future tasks in that repo reuse the saved team/project automatically.

To opt a repo out of Linear kickoff enforcement:

```bash
python3 plugins/linear-progress-sync/scripts/linear_start.py configure-repo \
  --root /path/to/repo \
  --disable-linear-sync \
  --reason "No Linear tracking"
```

Before kickoff, file edits, write-like Bash commands, and branch creation wait until active Linear state exists. Read-only and non-mutating Bash commands can run before kickoff.

After kickoff, Codex writes and standard Codex commits are synced back to the active Linear issue automatically.

Installed plugin caches check for updates during every `SessionStart`. The updater downloads the current `main.zip` archive, reads the plugin manifest from that archive, installs newer bootstrap versions into `~/.codex/plugins/cache/coreedge-local/linear-progress-sync/<version>/`, syncs coreedge-local marketplace plugins marked `INSTALLED_BY_DEFAULT`, and removes legacy global copies of native plugin hooks. Set `LINEAR_SYNC_AUTO_UPDATE=0` to disable automatic checks.

Existing installations before `0.2.11` install `0.2.11` on the next SessionStart; the following SessionStart runs the updated session logger and starts its resumable historical backfill. Fresh setup installs the current plugins immediately.

To force a manual update check:

```bash
python3 ~/.codex/plugins/cache/coreedge-local/linear-progress-sync/0.2.11/scripts/update_plugin.py --force
```

## Rolling Out Updates

Teammates install this repository once with `setup.py`. After that, they should not need to reinstall from the marketplace for normal plugin, skill, command, hook, or extension updates.

To roll out a new default plugin or extension:

1. Add or update the plugin under `plugins/<name>/`.
2. Set a new version in `plugins/<name>/.codex-plugin/plugin.json`.
3. Add the plugin to `.agents/plugins/marketplace.json`.
4. Set its marketplace policy to `INSTALLED_BY_DEFAULT`.
5. Bump `plugins/linear-progress-sync/.codex-plugin/plugin.json` and `plugins/linear-progress-sync/update-manifest.json`.
6. Merge to `main`.

After an updater release, the first new Codex thread or session installs the newer plugin. The following SessionStart runs that installed updater and removes legacy Core Edge entries from `~/.codex/hooks.json` so native plugin hooks run once.

## Optional

Only install the repo Git hook if you also want commits made outside Codex to sync to Linear:

```bash
python3 plugins/linear-progress-sync/scripts/setup.py --with-git-hook --root /path/to/repo
```

Inspect local sync state in a project:

```bash
find .codex/linear-sync -maxdepth 3 -type f -print
```
