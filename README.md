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

Start a coding task normally. Before the first Codex file edit, Codex must create or confirm the Linear issue, create the Linear-named branch, push an empty kickoff commit, open a draft PR, link Linear and GitHub, and write local active state.

Linear kickoff enforcement only applies to repos whose `origin` remote is under the `e3-solutions` GitHub org. Repos with another GitHub org are treated as out of scope and are allowed without Linear kickoff.

The first time the plugin is used, Codex lists Linear workspace users, asks you to choose your Linear user from that list, and saves the selected name in `~/.codex/linear-sync/user.json`. Future repos reuse that profile to assign newly created Linear issues and to add deterministic attribution to Linear issue bodies and comments.

The first time a repo needs a new Linear issue, Codex lists Linear teams/projects, asks you to choose the project from that list, and saves it in `~/.codex/linear-sync/repos.json`. Future tasks in that repo reuse the saved team/project automatically.

New Linear issues created by Codex must be assigned to the saved Linear user and use a `What`, `Why`, and `How` description. `What` should describe the expected non-technical business outcome, `Why` should explain the user or business pain with evidence, and `How` should briefly outline the technical approach. Codex also appends `Codex bot: <stored Linear user name> at <ISO-8601 UTC timestamp>` to Linear issue bodies and comments it creates.

To opt a repo out of Linear kickoff enforcement:

```bash
python3 plugins/linear-progress-sync/scripts/linear_start.py configure-repo \
  --root /path/to/repo \
  --disable-linear-sync \
  --reason "No Linear tracking"
```

Before kickoff, Codex file edits through `apply_patch` wait until active Linear state exists. General Bash commands are allowed before kickoff.

After kickoff, Codex writes and standard Codex commits are synced back to the active Linear issue automatically.

Installed plugin caches check for updates during every `SessionStart`. The updater installs newer bootstrap versions into `~/.codex/plugins/cache/coreedge-local/linear-progress-sync/<version>/`, syncs coreedge-local marketplace plugins marked `INSTALLED_BY_DEFAULT`, and refreshes global Codex hook registrations. Set `LINEAR_SYNC_AUTO_UPDATE=0` to disable automatic checks.

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
