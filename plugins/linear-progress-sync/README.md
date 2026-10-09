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
codex mcp login e3-cosmos
```

Then restart Codex or start a new Codex thread. If Codex asks to review hooks, trust the Linear Progress Sync and Codex Session Logging hooks once.

`setup.py` checks GitHub CLI auth, installs Linear Progress Sync and Codex Session Logging, removes legacy Core Edge hook copies from `~/.codex/hooks.json`, and registers Linear MCP and E3 Cosmos (`e3-cosmos`, unless a Cosmos server already exists under any name). It does not log you in to either, so `codex mcp login linear` and `codex mcp login e3-cosmos` are still required.

Linux requires a systemd-based distribution with a working user service manager. Setup writes the updater units under `$XDG_CONFIG_HOME/systemd/user` when that variable is set and `~/.config/systemd/user` otherwise, then enables the updater timer without requiring root. On a headless VM where updates must continue after logout, an administrator should enable lingering for the teammate account before setup:

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

Before kickoff, file edits, write-like Bash commands, and branch creation wait until active Linear state exists. Read-only and non-mutating Bash commands can run before kickoff. Repository synchronization commands such as `git fetch` and `git pull` can also run before kickoff.

After kickoff, successful Git commits are synced back to the active Linear issue automatically. File edits and session completion never create progress comments.

Setup installs a resident updater under `~/.codex/coreedge`. It runs independently of Codex tasks through a macOS LaunchAgent or Linux systemd user timer shortly after login and every 30 minutes. It downloads the current `main.zip`, validates and stages the default marketplace plugins, switches `~/.codex/coreedge/marketplace/current` atomically, points the registered marketplace at that stable path, and leaves only the selected version visible in each Codex plugin cache. Previous versions move to `~/.codex/coreedge/rollback/cache` so a failed activation can restore the prior state. `SessionStart` and `PreToolUse` repair a missing resident service without delaying or blocking Codex.

Existing installations download and activate `0.3.17` during one ordinary resident check. This release adds bounded Sesh retrieval cues at startup and after compaction, alongside the optional evidence-first Sesh search skill in Codex Session Logging `0.2.18` and preserves the existing Forum guidance with optional explained account feedback and manual librarian guidance, while continuing to require a reusable lesson in the full contribution, with applicability and limits, dated examples, and inspected prior work linked as supports, challenges, or supersedes. Agents query authoritative systems for current state, and useful learning is required before proposing a private candidate for approval. Guidance remains limited to startup, resume, clear/reset and post-compaction context rebuilds; ordinary prompt, tool, and completion hooks remain silent. The guidance hook never contacts Forum or submits automatically, skips non-E3 repositories, requires canonical GitHub HTTPS/SSH origins for new guidance, and honors `E3_COLLECTIVE_HOOK_ENABLED=0`. The release preserves the `0.3.10` repository synchronization guard, historical-backfill protections, Linux systemd support, commit-only Linear progress comments, complete hook-triggered parent and subagent rollout capture, and the monotonic usage RPC migration. Installations from older releases self-heal without rerunning setup; fresh setup installs and schedules the current plugins immediately.

Resident release `0.3.25` distributes Codex Session Logging `0.2.26`. The logger adds content-free Sesh yield linkage: ordered SHA-256 digests for bounded delivered source handles and strict `verified`, `failed`, or `unknown` source-open witnesses. It never retains the query, passage, source body, raw handle, or arbitrary tool input/output in queryable event metadata. Deploy the compatible ingest sanitizer before distributing the logger, then verify one excluded real-hook preflight before enrolling any yield cohort.

Resident release `0.3.34` distributes Codex Session Logging `0.2.34` (COR-4683). Every resident activation (fresh setup and each 30-minute cycle) now makes sure Codex has the E3 Cosmos MCP server, so agents keep Sesh search. Before this, nothing in the released plugins registered Cosmos: the COR-4144 setup step was dropped when that branch was merged on Sep 16, 2026, so existing users only had Cosmos if they added it by hand, and nothing put it back if it went missing. When no enabled MCP server points at the Cosmos gateway (`cosmos.e3g.ai`, any name such as `e3`, `cosmos` or `cosmos-e3`), the updater appends `[mcp_servers.e3-cosmos]` with the sign-in door for the git email's org (`https://cosmos.e3g.ai/coreedge/mcp` for `coreedgesolution.com`, otherwise `https://cosmos.e3g.ai/e3/mcp`). It never edits an existing entry (a custom `e3-cosmos` URL or command, or `enabled = false`, is left as the user's choice), never rewrites other sections, writes only if `config.toml` did not change since it was read, skips configs it cannot parse safely, and reports only a status word. Opt out with `python3 plugins/linear-progress-sync/scripts/update_plugin.py --disable-cosmos-mcp` (or `E3_COSMOS_MCP_AUTO_REGISTER=0`, persisted once seen); `--enable-cosmos-mcp` turns it back on, and `--disable-auto-update` still stops everything. A failed activation now undoes only its own `config.toml` write instead of restoring the whole earlier snapshot, which could erase entries Codex or the user wrote meanwhile. The logger adds a health signal: at `SessionStart` in E3 repositories it queues one content-free `codex_session_events` row when the Cosmos status changes (`cosmos_mcp_missing`, `cosmos_mcp_disabled`, `cosmos_mcp_custom`, `cosmos_mcp_unreadable`, then `cosmos_mcp_restored`); no config content is sent. Newly registered users run `codex mcp login e3-cosmos` once if Codex asks for sign-in.

Resident release `0.3.33` distributes Codex Session Logging `0.2.33` (COR-4681). It adds a Sesh startup-cue on/off experiment that is **off by default**; nothing changes until `E3_SESH_CUE_EXPERIMENT=1` is set, and it will be announced before anyone turns it on. When on, each human Codex session in an e3-solutions repository is put 50/50 into `cue` or `no_cue` by `sha256(ISO week + ":" + session id)`, with the week taken from the UUIDv7 session id so compaction keeps the arm. `E3_SESH_EXPERIMENT_OPTOUT=1` always keeps the cue (`optout`), automated threads always keep it (`bot`), and `E3_SESH_CONTEXT_ENABLED=0` still removes it (`disabled`). The arm is logged on the `SessionStart` event as `metadata.sesh_cue_experiment`, `metadata.sesh_cue_arm` and `metadata.sesh_cue_salt`. Deploy the matching ingest sanitizer before turning the flag on; the previous sanitizer drops the new keys. See the Codex Session Logging README.

Resident release `0.3.32` distributes Codex Session Logging `0.2.32` (COR-4646). The logger again links every Codex Sesh search to its `sesh_usage.events` request: from Oct 5, 2026 about half of searches lost `metadata.sesh_request_id` because uncompacted answers passed the 256 KB parse cap (the id is the last key of the answer). It now parses answers up to 16 MB and, above that, reads only structural `search_request_id` keys (never escaped text inside passages, up to 64 MB). It matches any Sesh search tool name (`mcp__<namespace>__sesh__search_coding_sessions`, `mcp__<namespace>__search_coding_sessions` or the bare name, including `mcp__codex_apps__cosmos__...` and `mcp__e3_mcp__...`), and records every id of a multi-question (`queries[]`) call as `metadata.sesh_request_ids` (primary first; `sesh_request_id` is unchanged). Deploy the matching ingest sanitizer first; the previous sanitizer stays compatible and simply drops the new namespaces and `sesh_request_ids`.

Resident release `0.3.31` distributes Codex Session Logging `0.2.31`, and Claude Session Logging `0.2.18` ships the same Forum decision cue to Claude Code through the `coreedge-internal` marketplace (its own daily auto-update, not the resident updater). The cue now asks for 2-4 words naming the system, tool or component plus the failure or decision (e.g. "OCR ensemble evaluation", "SMS receipt auth failure", "release branch ancestry") instead of abstract mechanism words, and notes that Forum holds internal lessons, not vendor capability facts; in production, queries naming the system were useful ("OCR ensemble evaluation" 10/10, "release branch ancestry" 2/2) while abstract ones such as "bounded delivery" or "formatting checks" mostly returned posts that did not apply. It still excludes ticket IDs, file paths, function names and known post titles. Search results now include a `search_id`, and the cue asks agents to pass it with each `give_collective_feedback` assessment. In Claude Code the `UserPromptSubmit` hook uses the same gating (canonical E3 repositories only; a session's first prompt, then a new task of 25+ words at most once an hour), prints the cue as `hookSpecificOutput.additionalContext` before the existing prompt capture, logs only the session id, time, reason and prompt word count under `~/.claude/session-logging/forum-cue` (or `$CLAUDE_SESSION_LOG_STATE_DIR/forum-cue`), never prompt text, and honors `FORUM_CUE_ENABLED=0`.

Resident release `0.3.30` distributes Codex Session Logging `0.2.30`. The Forum decision cue now takes two calls per useful post: search, then one `give_collective_feedback` (up/down/abstain) using the `post_id` and `body_sha256` already in the search result, with `mutation_id` optional. It no longer asks for a separate `get_research_post`, guide or status call, a related-mode retry (Forum fills and broadens short searches itself), or a `Forum: USE|SKIP` line; the final answer names the Forum posts used. Before this, a saved assessment took 5.4 Forum calls. Requires Forum with e3-solutions/forum#36.

Resident release `0.3.29` stops Linear Progress Sync's `codex exec` worker from running a Sesh prior-work search at session start (it sets `E3_SESH_CONTEXT_ENABLED=0` for the worker only). In a Sep 18-23 audit these automated runs were about 20% of linked Sesh searches and never used the results. Codex Session Logging stays at `0.2.29`.

Resident release `0.3.28` distributes Codex Session Logging `0.2.29`. The `Stop` hook now also records the turn's progress updates (assistant `commentary` messages) as assistant messages before the final answer, tagged `metadata.message_phase` = `commentary` or `final_answer`. Agents state most concrete findings (numbers, causes, file names) in these updates, and Sesh previously indexed only final answers. It reads only transcript bytes appended since the previous `Stop` (an 8 MB tail on first use), caps 50 updates per turn, never records tool input or output, and keeps the existing E3-repository gate. Disable it with `CODEX_SESSION_LOG_COMMENTARY=0`. Sessions listed in `~/.codex/session-logging/excluded_sessions.json` or the Sherlock exclusion file (`~/.sherlock/session-exclusions.json`), and every subagent descended from them, are never captured or rollout-synced.

Resident release `0.3.26` distributes Codex Session Logging `0.2.27`. In canonical E3 repositories the `UserPromptSubmit` hook now adds a short Forum decision cue on a thread's first prompt, and on a new task of 25+ words at most once an hour. It asks for one targeted `forum__search_research_posts` (2-4 plain words, retrying once in `related` mode) before the agent commits to an approach, plus a one-line `Forum: USE|SKIP <post_id> - reason` or `Forum: NONE - query`, so Forum yield can be measured from ordinary assistant messages. The hook itself never contacts Forum. Its local log under `~/.codex/forum-cue` records only the session id, time and prompt word count, never prompt text. Disable it with `FORUM_CUE_ENABLED=0`.

Resident release `0.3.27` distributes Codex Session Logging `0.2.28`. Session-start, post-compaction context rebuild, and decision-time Forum cues now require one explained `up`, `down`, or neutral `abstain` assessment after each fully read post and before task completion when feedback is enabled and authorized. The exact post id/body hash and a fresh mutation UUID are required; uncertain retries reuse the identical payload and UUID, definitive failures remain explicitly unsaved, optional concerns stay separate, and USE/SKIP prose is never treated as a saved event. `Stop` remains telemetry-only.

The Sesh retrieval cue is independent of Forum guidance. Disable it with
`E3_SESH_CONTEXT_ENABLED=0` (the compatibility alias
`E3_SESH_START_SEARCH_ENABLED=0` is also honored). Disabling Collective alone
does not disable Sesh. These cue settings must be inherited by the hook process.

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

`update_plugin.py --doctor` verifies that the resident update LaunchAgent or systemd user timer is installed and active. Session capture health is represented by the durable local queue and advances on Codex hooks rather than a background timer.

## Optional

Only install the repo Git hook if you also want commits made outside Codex to sync to Linear:

```bash
python3 plugins/linear-progress-sync/scripts/setup.py --with-git-hook --root /path/to/repo
```

Inspect local sync state in a project:

```bash
find .codex/linear-sync -maxdepth 3 -type f -print
```
