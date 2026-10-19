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
