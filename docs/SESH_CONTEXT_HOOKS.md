# Sesh context hooks — COR-4025

Codex Session Logging 0.2.18 emits a short retrieval cue on SessionStart with
source startup or compact, only in repositories with a canonical e3-solutions
GitHub origin. The scope hint is not authorization. The existing caller-bound
Sesh service still enforces access and result limits.

The cue asks for one relevant search for the current coding task, waits for a
task when startup has none, respects user opt-out, and continues ordinary work
on missing tools or failure. It instructs the agent not to retry without a user
request and to verify actual passage support. Prompt guidance is not a guarantee
of exactly-once execution, complete logging, or useful answers.

Either E3_SESH_CONTEXT_ENABLED=0 or E3_SESH_START_SEARCH_ENABLED=0 disables the
cue (false/no/off also work). These are inherited environment settings; this
release does not add a persistent settings store. Resume/clear cues are unchanged.
Existing Forum guidance, session capture and rollout sync are preserved. This
hook adds no network call, question capture, credential handling or grant.
Teammate question text remains governed by the existing server allowlist.

The resident updater version 0.3.17 distributes this release through existing
installations. Publication does not prove every teammate has updated, enabled,
or trusted the hook. Verify one real teammate startup/compaction and search,
source opening and receipt before claiming acceptance. Never send Slack as part
of the hook workflow.

Rollback: publish a newer coordinated release removing the added Sesh context
call/module, or disable the cue locally through the environment setting.
Existing updater activation rollback copies remain available. No SQL rollback
or access change is needed for this hook-only release.

Validation: focused context tests cover startup/compact, opt-outs, non-E3 scope,
bounded git failures and unchanged Forum/capture/sync execution when cue creation
fails. Upstream updater/Forum tests also run before publication. These are not
live fleet adoption tests.

## Update: startup only (COR-4688, Codex Session Logging 0.2.35)

The cue is no longer emitted for source compact, for any thread including bots.
E3_SESH_CUE_AFTER_COMPACTION=1 (true/on) restores the previous cue text at
compaction. The startup cue now asks for one search using the user's actual task
and opening the top result with the exact returned source arguments before the
first edit. Rollback: set the variable, or publish a release reverting COR-4688.

## Update: routine-cue A/B (COR-4808, Codex Session Logging 0.2.36, on by default)

On by default (E3_SESH_ROUTINE_AB=0/false/no/off turns it off). It withholds the startup cue, and the compaction cue
if it is turned back on, for the half of human sessions whose arm is need_only.
Arm = low bit of sha256("sesh_routine_v1:" + lowercased session id), first byte;
1 = need_only. Bots, opt-outs and unreadable threads keep the cue. Sesh tools,
the skill and search logging are unchanged, and nothing new is logged. With the
flag unset the hook is identical to 0.2.35. Rollback: unset the variable.

## Update: fire on need (COR-4811, Codex Session Logging 0.2.37, on by default since 0.2.38)

Both switches are on by default since 0.2.38 (Oct 16, 2026); setting either variable to
0/false/no/off turns that one off.
E3_SESH_ONE_PER_TURN denies a second Sesh coding-session search in the same turn
(PreToolUse, keyed on session and turn id); opening, find-similar and ask_sesh are never
blocked, bots and opt-outs are never limited, and errors allow the call.
E3_SESH_PAST_WORK_CUE adds a one-line UserPromptSubmit cue to search Sesh once when a
person's message in an E3 repository asks about earlier work (the study's tight pattern).
With both set to 0 the hooks are identical to 0.2.36. Rollback: set the variables to 0.

## Update: standalone `sesh` plugin (COR-4884, sesh 0.1.0)

The same hooks also ship as their own Codex plugin, `plugins/sesh`. This is because Jolly
Roger #15/#16 retires Codex Session Logging, which turned them off on retired machines.

- **Same code and defaults.** The cue, routine A/B, past-work cue, guard and Sesh-only linking
  events are the code from 0.2.38, with the same defaults. A differential test runs both
  plugins' real hooks and checks that their output and Sesh events match.
- **No hook fires twice.** When an active Codex Session Logging will run a hook, the plugin
  does nothing for that event. Codex Session Logging is unchanged.
- **Plugin version is recorded per session.** Uploads name `plugin: sesh`, and the session's
  `environment_snapshot` lists the `sesh` skill and its version. No ingest or schema change.
- **Shipping:** through the jolly-roger #10 native plugin catalog. See `plugins/sesh/README.md`.

## Update: existing-work plan cue and 15% session holdout (COR-4887; sesh 0.2.0, Codex Session Logging 0.2.40)

Both Codex copies run the same code (`sesh_context.py`, `sesh_need.py`, `plan_cue.py` and
`sesh_hooks.py` are byte-identical). The rules match the Claude Code plugin (`plugins/claude-sesh`,
COR-4888), and `tests/test_sesh_plan_cue_holdout.py` checks that.

**Plan cue (`E3_SESH_PLAN_CUE`, on by default; set it to 0 to turn it off)**
- **When it fires.** PreToolUse on `apply_patch` when the patch adds a new code file to a person's
  session in an e3-solutions repository. The file must count as a new module, service, script or
  migration.
- **What it says.** It adds model-visible context and never blocks the patch. The agent is told to
  make one Sesh search for existing or teammate work and show any match as an existing-work card
  (link, owner, date, and one line on what that work did). Then the agent reuses or extends it, or
  says in one line why not. The search results already carry each result's glance (when, whose,
  outcome) and `by_owner`, so the server needed no change.
- **How often.** At most twice a session, and once per area. An area is the file's first two
  folders, keyed by the repository's origin. It never fires in a turn that already ran a Sesh search.
- **Paths it skips.** Tests, fixtures, generated, vendored and scratch paths never count. Neither do
  output, artifact, review, analysis, docs and data folders, or ticket- and date-named folders
  (`PLAN_SKIP_PATH`, shared with claude-sesh).
- **Continuation rule (Codex only).** A new file in a folder this session has already changed is a
  continuation of work under way, and never cues. Codex sees every patch, so it can tell.
- **Bots.** Automated threads get no plan cue, the same as Claude Code. They keep every other Sesh
  cue and all tools.

**Holdout (`E3_SESH_HOLDOUT_PCT`, default 15)**
- **Which sessions.** A session is held out when `(b[0]*256 + b[1]) % 100 < pct`, where
  `b = sha256("sesh_holdout_v1:" + lower(trim(session id)))`. This is the same formula as Claude Code.
- **What a held-out session gets.** No startup cue, past-work cue or plan cue, and a second
  search in a turn is never denied. The guard's turn marker is still kept, so "this turn already
  searched" means the same in both arms. Sesh tools, the skill and all logging stay. Claude Code
  applies the same rule.
- **Who is never held out.** Bots, opt-outs and sessions with no id. Codex also needs the
  transcript header and logs `unassigned` without it; Claude Code needs only the session id.
- **How it combines with the routine A/B.** The holdout is decided first. The routine A/B
  (`E3_SESH_ROUTINE_AB`) then splits the treated 85% with its own independent hash, which stays
  about 50/50.
- **Assignment logs.** `sesh_arm` is logged as holdout, treated, bot, optout or unassigned. It goes
  on every `environment_snapshot` and Sesh tool event, with `sesh_routine_arm`, `sesh_holdout_pct`
  and `sesh_plugin_version`.
- **Exposure logs.** Exposure is logged separately, one event per cue moment:
  `sesh_startup_cue_shown|withheld`, `sesh_past_work_cue_shown|withheld` and
  `sesh_plan_cue_shown|withheld`. The withheld events carry `sesh_withheld_by: holdout|need_only`.
- **Path and text.** No path, prompt or query is logged. The plan topic is a sha256.

**One-search guard logging (reviewer item, Oct 20)**
- **Denial text.** The denial now states the configured limit neutrally. The Oct 16 evidence was
  39 no and 12 unknown, measured on finished searches, which is not proof of no harm.
- **What a denial records.** Every denial is logged with `sesh_turn_guard: denied` and
  `sesh_turn_guard_first: succeeded|failed|unknown`, which records how that turn's first search
  ended. The event also carries the turn id.

**Ingest.** The deployed `codex-session-ingest` drops labels it does not know. In 4 days of prod
events, none carried `sesh_turn_guard` or `sesh_cue_arm`, so the Oct 16 "0 guard denials" count
was a logging artifact. `event_sanitizer.ts` now allowlists the labels above as enums, small
integers or hashes. It must be deployed before the labels land. Until then the event types still
land, and the arm can be recomputed from the session id with the formula above.
