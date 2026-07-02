# Linear Progress Sync

Local Codex plugin for Linear-first development. Codex starts work from a Linear issue, creates the linked branch and draft PR before code, then keeps Linear updated from Codex commits.

## Setup

For a new teammate:

```bash
git clone https://github.com/e3-solutions/codex-plugins
cd codex-plugins
```

Authenticate GitHub CLI:

```bash
gh auth login
```

Run setup:

```bash
python3 plugins/linear-progress-sync/scripts/setup.py
```

Authenticate Linear after setup registers the MCP server:

```bash
codex mcp login linear
```

Then restart Codex or start a new Codex thread.

Preview setup without changing Codex config:

```bash
python3 plugins/linear-progress-sync/scripts/setup.py --dry-run
```

## What Setup Does

- checks GitHub CLI auth with `gh auth status`
- adds this repo as a Codex plugin marketplace
- installs `linear-progress-sync@coreedge-local`
- configures Linear MCP with `codex mcp add linear --url https://mcp.linear.app/mcp`

This is user-level setup. You do not need to install anything separately in every repo for normal Codex work.

## Normal Use

Start a coding task in Codex. The plugin makes Codex run Linear kickoff before edits: Linear issue, branch, empty kickoff commit, draft PR, and local active state.

The first time a repo needs a new Linear issue, Codex asks which Linear team/project that repo should use and saves it in `~/.codex/linear-sync/repos.json`. Future tasks in that repo reuse the saved team/project automatically.

Standard Codex commits are then synced back to the active Linear issue automatically.

## V1 Scope

The automatic commit sync covers the normal `git commit ...` flow Codex uses. The optional repo hook covers commits made outside Codex. Hardening unusual wrapped Git invocations such as `/usr/bin/git commit` or `git -C <repo> commit` is a follow-up.

## Optional

Only if you also want commits made outside Codex to sync to Linear:

```bash
python3 plugins/linear-progress-sync/scripts/setup.py --with-git-hook --root /path/to/repo
```

Inspect local sync state in a project:

```bash
find .codex/linear-sync -maxdepth 3 -type f -print
```
