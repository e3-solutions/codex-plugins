# Linear Progress Sync

Local Codex plugin for Linear-first development. Codex starts work from a Linear issue, creates the linked branch and draft PR before code, then keeps Linear updated from Codex commits.

## Setup

Run this once per teammate, not once per repo:

```bash
git clone https://github.com/e3-solutions/codex-plugins
cd codex-plugins
gh auth login
python3 plugins/linear-progress-sync/scripts/setup.py
codex mcp login linear
```

Then restart Codex or start a new Codex thread. If Codex asks to review hooks, trust the Linear Progress Sync hooks once.

`setup.py` checks GitHub CLI auth, installs the plugin, installs global Codex hooks into `~/.codex/hooks.json`, and registers Linear MCP. It does not log you in to Linear, so `codex mcp login linear` is still required.

Preview setup without changing Codex config:

```bash
python3 plugins/linear-progress-sync/scripts/setup.py --dry-run
```

## Normal Use

Start a coding task normally. Before the first edit or branch creation, Codex must create or confirm the Linear issue, create the Linear-named branch, push an empty kickoff commit, open a draft PR, link Linear and GitHub, and write local active state.

The first time the plugin is used, Codex lists Linear workspace users, asks you to choose your Linear user from that list, and saves the selected name in `~/.codex/linear-sync/user.json`. Future repos reuse that profile to assign newly created Linear issues and to add deterministic attribution to Linear issue bodies and comments.

The first time a repo needs a new Linear issue, Codex lists Linear teams/projects, asks you to choose the project from that list, and saves it in `~/.codex/linear-sync/repos.json`. Future tasks in that repo reuse the saved team/project automatically.

To opt a repo out of Linear kickoff enforcement:

```bash
python3 plugins/linear-progress-sync/scripts/linear_start.py configure-repo \
  --root /path/to/repo \
  --disable-linear-sync \
  --reason "No Linear tracking"
```

Before kickoff, Bash uses a read-only allowlist. Simple inspection commands like `pwd`, `ls`, `cat`, `rg`, `grep`, `stat`, and read-only `git` commands work. Unknown scripts, tests, builds, file writes, and branch creation wait until active Linear state exists.

After kickoff, Codex writes and standard Codex commits are synced back to the active Linear issue automatically.

Installed plugin caches check for updates during `SessionStart` at most every six hours. The updater installs newer bootstrap versions into `~/.codex/plugins/cache/coreedge-local/linear-progress-sync/<version>/`, syncs coreedge-local marketplace plugins marked `INSTALLED_BY_DEFAULT`, and refreshes global Codex hook registrations. Set `LINEAR_SYNC_AUTO_UPDATE=0` to disable automatic checks.

To force a manual update check:

```bash
python3 ~/.codex/plugins/cache/coreedge-local/linear-progress-sync/0.2.2/scripts/update_plugin.py --force
```

## Optional

Only install the repo Git hook if you also want commits made outside Codex to sync to Linear:

```bash
python3 plugins/linear-progress-sync/scripts/setup.py --with-git-hook --root /path/to/repo
```

Inspect local sync state in a project:

```bash
find .codex/linear-sync -maxdepth 3 -type f -print
```
