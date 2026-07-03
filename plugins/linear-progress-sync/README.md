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

The first time the plugin is used, Codex asks for your Linear name and saves it in `~/.codex/linear-sync/user.json`. Future repos reuse that profile to assign newly created Linear issues and to add deterministic attribution to Linear issue bodies and comments.

The first time a repo needs a new Linear issue, Codex asks which Linear team/project that repo should use and saves it in `~/.codex/linear-sync/repos.json`. Future tasks in that repo reuse the saved team/project automatically.

Before kickoff, Bash uses a read-only allowlist. Simple inspection commands like `pwd`, `ls`, `cat`, `rg`, `grep`, `stat`, and read-only `git` commands work. Unknown scripts, tests, builds, file writes, and branch creation wait until active Linear state exists.

After kickoff, Codex writes and standard Codex commits are synced back to the active Linear issue automatically.

## Optional

Only install the repo Git hook if you also want commits made outside Codex to sync to Linear:

```bash
python3 plugins/linear-progress-sync/scripts/setup.py --with-git-hook --root /path/to/repo
```

Inspect local sync state in a project:

```bash
find .codex/linear-sync -maxdepth 3 -type f -print
```
