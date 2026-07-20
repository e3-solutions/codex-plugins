# Linear Progress Sync

Local Codex plugin for Linear-first development in `e3-solutions` repos. Codex starts work from a Linear issue, creates the linked branch and draft PR before code, then keeps Linear updated from Codex commits.

## Agent Install Contract

This repository is a Codex plugin marketplace, not a single plugin source. Do not install the GitHub URL or repository root directly with `codex plugin add`.

Agents installing this for a teammate must clone the repo and run `setup.py`, because setup registers the marketplace, installs both required plugins, removes legacy duplicate global hooks, and registers Linear MCP.

Current behavior:

- Enforcement is scoped to git repos whose `origin` remote is under `e3-solutions/*`.
- Repos with no `origin` remote, no git repo, or a non-E3 origin are out of scope and file edits are allowed without Linear kickoff.
- Before kickoff in scoped repos, only file edits, write-like Bash commands, and branch creation are blocked. Read-only inspection and non-mutating commands are allowed.
- Linear progress comments are created only after Git commits; file edits and session completion do not post progress comments.
- A resident updater checks at login and every 30 minutes through a macOS LaunchAgent or Linux systemd user timer, atomically activates newer default marketplace plugins, and retains `SessionStart` as a self-healing fallback.

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

Linux requires a systemd-based distribution with a working user service manager. Setup writes user units under `$XDG_CONFIG_HOME/systemd/user` when that variable is set and `~/.config/systemd/user` otherwise, then enables the updater and task-presence timers without requiring root. On a headless VM where these timers must continue after logout, an administrator should enable lingering for the teammate account before setup:

```bash
sudo loginctl enable-linger "$USER"
```

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

After kickoff, successful Git commits are synced back to the active Linear issue automatically. File edits and session completion never create progress comments.

Setup installs a resident updater under `~/.codex/coreedge`. It runs independently of Codex tasks through macOS LaunchAgents or Linux systemd user timers: once shortly after login, every 30 minutes for updates, and every minute for metadata-only task presence. It downloads the current `main.zip`, validates and stages the default marketplace plugins, switches `~/.codex/coreedge/marketplace/current` atomically, points the registered marketplace at that stable path, and leaves only the selected version visible in each Codex plugin cache. Previous versions move to `~/.codex/coreedge/rollback/cache` so a failed activation can restore the prior state. `SessionStart` and `PreToolUse` repair a missing resident service without delaying or blocking Codex.

Existing macOS installations download and activate `0.3.3` during one ordinary resident check. The release preserves historical-backfill protections, the metadata-only one-minute task-presence publisher, and commit-only Linear progress comments while adding Linux systemd user timers. Linux installations from before `0.3.3` had no resident scheduler and must run the repository setup flow once to install the timers; after that, updates are automatic. Fresh setup installs and schedules the current plugins immediately. Presence expires observations older than five minutes instead of replaying false activity after an outage and does not depend on an already-running Codex process reloading hooks. An already-running task keeps the skill text it loaded at creation, while hook implementations switch to the selected cache automatically and presence continues independently.

Persistently disable or re-enable automatic network update checks with:

```bash
python3 ~/.codex/coreedge/runtime/current/update_plugin.py --disable-auto-update
python3 ~/.codex/coreedge/runtime/current/update_plugin.py --enable-auto-update
```

`LINEAR_SYNC_AUTO_UPDATE=0` is also honored. When setup or a self-healing hook observes it, the opt-out is saved for future resident launches that do not inherit the shell environment.

To force a manual update check:

```bash
python3 ~/.codex/coreedge/runtime/current/update_plugin.py --force
```

Inspect updater health and activation drift:

```bash
python3 ~/.codex/coreedge/runtime/current/update_plugin.py --doctor
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

After the one-time setup, releases are staged and activated by the resident service. No teammate reinstall, update command, or renewal thread is part of the rollout. A normal future task naturally picks up changed skill text; existing hook events use the sole selected cache version.

`update_plugin.py --doctor` also verifies that the updater and metadata-only task-presence LaunchAgents or systemd user timers are installed and active.

## Optional

Only install the repo Git hook if you also want commits made outside Codex to sync to Linear:

```bash
python3 plugins/linear-progress-sync/scripts/setup.py --with-git-hook --root /path/to/repo
```

Inspect local sync state in a project:

```bash
find .codex/linear-sync -maxdepth 3 -type f -print
```
