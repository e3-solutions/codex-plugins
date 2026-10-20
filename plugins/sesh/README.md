# Sesh (Codex plugin)

The agent-side Sesh hooks as their own plugin (COR-4884). Until 0.1.0 they shipped only inside
Codex Session Logging, so retiring that logger (jolly-roger #15/#16, Oct 16, 2026) turned them
off. Sesh itself (the corpus, Sherlock ingestion, the Cosmos tools) never depended on them.

## What it does

Since 0.2.0 (COR-4887) the shared code is the same as Codex Session Logging 0.2.40, with the same flags and defaults:

| Hook | Behaviour | Switch (default) |
|---|---|---|
| `SessionStart` | Startup "Sesh prior-work context" cue in e3-solutions repositories. Not after compaction. | `E3_SESH_CONTEXT_ENABLED=0` removes it; `E3_SESH_CUE_AFTER_COMPACTION=1` brings back the compaction cue |
| `SessionStart` | COR-4808 routine A/B: human sessions in arm `need_only` get no routine cue | `E3_SESH_ROUTINE_AB` (on) |
| `SessionStart` | COR-4681 cue on/off experiment | `E3_SESH_CUE_EXPERIMENT` (off) |
| `UserPromptSubmit` | One-line cue to search Sesh once when a person asks about earlier work | `E3_SESH_PAST_WORK_CUE` (on) |
| `PreToolUse` | One Sesh coding-session search per turn; the second is denied (logged with the first search's outcome) | `E3_SESH_ONE_PER_TURN` (on) |
| `PreToolUse` | COR-4887 existing-work plan cue before `apply_patch` adds a new code file (at most 2 a session, once per area, never in a turn that searched; people only) | `E3_SESH_PLAN_CUE` (on) |
| all | COR-4887 holdout: this share of human sessions gets no Sesh cue and no guard; tools stay | `E3_SESH_HOLDOUT_PCT` (15) |
| `PreToolUse`/`PostToolUse` | Sesh search linking (below) | `E3_SESH_LINKING` (on) |

Bots and `E3_SESH_EXPERIMENT_OPTOUT=1` keep unlimited searches and no routine A/B, as before.
It also ships the `search-coding-sessions` skill the cue refers to.

## Search linking and version telemetry

For Sesh tools only (search, ask, find-similar, open, and the source-open tools its results
point to), the plugin queues the same metadata-only `tool_call_started`/`tool_call_finished`
events Codex Session Logging writes: `sesh_request_id(s)`, the origin session, source-handle
digests, open witnesses, and `sesh_turn_guard: denied`. At `SessionStart` it queues one
`environment_snapshot`. They go to the existing `codex-session-ingest` function, so the
`codex_session_events` readouts keep working unchanged.

- **Not sent:** prompts, assistant text, queries, passages, tool input or output, and other tools.
- **Same gates as the logger:** e3-solutions repositories only, excluded sessions, and the
  logger's upload opt-out (`CODEX_SESSION_LOG_AUTO_UPLOAD=0` or its saved preference).
- **Own state:** `~/.codex/sesh/session-logging` (`E3_SESH_STATE_DIR`). It reuses the
  logger's installation id if one exists, so the person keeps the same identity.

How we know who runs what:

- Every upload names `plugin: {name: "sesh", version}`, and its user-agent is `sesh/<version>`.
- The session's `environment_snapshot` lists the skill `search-coding-sessions` with
  `plugin: "sesh"`, `marketplace: "jolly-roger-sesh"` and the version. Today's ingest keeps
  that, so each linked search maps to a plugin version with no server change:

```sql
select sk->>'plugin' as plugin, sk->>'version' as version,
       count(distinct f.metadata->>'sesh_request_id') as searches
from codex_session_events f
join codex_session_events s on s.session_id = f.session_id and s.event_type = 'environment_snapshot'
cross join lateral jsonb_array_elements(s.metadata->'codex_setup'->'skills') sk
where f.event_type = 'tool_call_finished' and f.metadata ? 'sesh_request_id'
  and sk->>'name' = 'search-coding-sessions'
group by 1, 2;
```

## Installed next to Codex Session Logging

No hook fires twice. Every logger release already runs these hooks itself. So, for each event,
this plugin does nothing when the logger will run:

- the logger is enabled in `~/.codex/config.toml`, or its hooks are in `~/.codex/hooks.json`; and
- the logger's script for that event is in the plugin cache.

It runs everything when the logger is retired, disabled or missing. In that case the logger's
own `environment_snapshot` still records the `sesh` skill and version. The logger needs no
change.

`E3_SESH_HOOK_OWNER=sesh` or `E3_SESH_HOOK_OWNER=codex-session-logging` forces the choice, for
testing.

## Install by hand (until Jolly Roger delivers it)

One command, on a Mac or Linux machine with Codex, Python 3 and Git access to this repository:

```sh
(d="$(mktemp -d)" && trap 'rm -rf "$d"' EXIT && git clone -q https://github.com/e3-solutions/codex-plugins.git "$d/codex-plugins" && python3 "$d/codex-plugins/plugins/sesh/install.py")
```

Then start a new Codex thread, and trust the sesh hooks once if Codex asks.

`install.py` installs this plugin as pinned in the script (commit `c6411e1`, version `0.2.0`; COR-4887 plan cue and holdout), using
the same Codex commands as `setup.py`:

- It writes a private local marketplace `e3-sesh` at `~/.codex/sesh/marketplace/<commit>/` from
  `git archive` of that commit, then runs `codex plugin marketplace add` and `codex plugin add sesh@e3-sesh`.
  Codex puts the plugin in `~/.codex/plugins/cache/e3-sesh/sesh/0.2.0/` and enables it in `config.toml`.
- It is safe to rerun. A rerun repairs a disabled plugin, a missing cache or a changed marketplace
  copy, and otherwise changes nothing.
- It touches no other plugin or marketplace and checks that afterwards. It writes no `hooks.json`
  and installs no scheduler, so there are no automatic updates. Rerun it after a version bump.
- With or without Codex Session Logging. When the logger is active, sesh steps aside (above).
- If another `sesh` plugin is already installed (for example `sesh@jolly-roger-sesh`), it does nothing.
- Jolly Roger's legacy retirement removes only `@coreedge-local` plugins, the `coreedge-local`
  marketplace and its cache, so it leaves `sesh@e3-sesh` in place.
- `python3 plugins/sesh/install.py --uninstall` removes `sesh@e3-sesh`, the `e3-sesh` marketplace
  and its copy, and keeps the Sesh queue in `~/.codex/sesh/session-logging`.

When the Jolly Roger catalog starts delivering `sesh`, run `--uninstall` first: the catalog
refuses to add a plugin whose name is already installed from another marketplace.

The session's `environment_snapshot` then shows `marketplace: "e3-sesh"` for a by-hand install
and `"jolly-roger-sesh"` for a Jolly Roger one, so the query above tells them apart.

## Shipping through Jolly Roger (for Arya)

It is built for the native plugin catalog in jolly-roger #10:

- `.codex-plugin/plugin.json` has name `sesh`.
- No symlinks; every file is mode 644.
- Well under 512 files and 8 MiB.
- Not in the `coreedge-local` marketplace, so `legacy_uninstall.py` neither removes it nor
  stalls on it.

1. Land #9/#10 (rebased on main; it currently conflicts in `git_delivery.py` and `README.md`).
2. Add this to `jolly_roger/native_plugins.json`, with the commit of the codex-plugins `main`
   merge that contains this plugin (it must be an ancestor of `main`):

   ```json
   "sesh": {
     "repository": "https://github.com/e3-solutions/codex-plugins.git",
     "commit": "<40-hex merge commit on codex-plugins main>",
     "path": "plugins/sesh",
     "version": "0.1.0"
   }
   ```

   `version` must equal `plugins/sesh/.codex-plugin/plugin.json` at that commit. Future
   releases bump both fields.
3. Do **not** also add `plugins/sesh/skills/search-coding-sessions` to the #9 skills catalog
   for Codex, because the plugin already ships it. A Claude-only #9 entry is fine if wanted.
4. Run `python3 -m unittest discover -s tests` in jolly-roger, then one real install. Expect:
   - `codex plugin add sesh@jolly-roger-sesh` succeeds;
   - `~/.codex/plugins/cache/jolly-roger-sesh/sesh/0.1.0/` exists;
   - Codex asks to trust the new hooks once in the next session.
5. Verify on one retired machine:
   - a new Codex thread in an e3-solutions repo shows the cue (or not, for a `need_only` session);
   - that session's `environment_snapshot` lists `plugin: "sesh"`;
   - its Sesh searches have `tool_call_finished` rows with `sesh_request_id`.

Rollback: remove the catalog entry (Jolly Roger runs `codex plugin remove`), or set
`E3_SESH_CONTEXT_ENABLED=0`, `E3_SESH_ONE_PER_TURN=0`, `E3_SESH_PAST_WORK_CUE=0` and
`E3_SESH_LINKING=0` on the machine.
