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

## Automated runs and per-turn cap — COR-4593 (Codex Session Logging 0.2.32)

The Sep 24 - Oct 1 yield study found 65 Sesh searches from automated runs and
none helped, so `scripts/sesh_gate.py` now classifies runs:

| Run type | Signal |
|---|---|
| Any worker we launch (Linear Progress Sync `codex exec`) | `E3_AUTOMATED_AGENT=1` |
| `codex exec` with a rollout | session_meta `originator=codex_exec` or `source=exec` |
| Codex approval-review subagent | `thread_source=guardian_review` / `source.subagent.other=guardian` |
| Codex heartbeat automation turn | prompt starts with `<heartbeat>` or carries `<automation_id>` |
| Codex suggestion generator (ephemeral, no rollout) | prompt template "Generate N to M hyperpersonalized suggestions" |
| Linear Progress Sync worker | prompt template "You are Linear Progress Sync running inside Codex." |
| Claude Code codex plugin review gate / adversarial review | prompt templates from its `prompts/` |

Environment and rollout signals hold for the whole session. Prompt templates
hold only for that turn, so a heartbeat turn inside a person's thread does not
change the person's later turns. Ephemeral runs write no rollout, and the hook
payload carries no originator, so the suggestion generator can only be
recognised from its prompt. That is why the startup cue is now deferred to the
first `UserPromptSubmit`: at bare startup a person's thread and a template run
look identical.

Automated runs get no cue at startup, first prompt or compaction, and the
`PreToolUse` hook blocks their `sesh__search_coding_sessions` calls. For a
person's turn the hook allows 2 searches and blocks the rest until the next
user prompt (or new `turn_id`); the cue states the cap. Blocking uses exit code
2 with the reason on stderr, the same mechanism Linear Progress Sync uses for
its write guard.

Controls: `E3_SESH_MAX_SEARCHES_PER_TURN` (default 2, `0` = no cap),
`E3_SESH_SEARCH_GATE_ENABLED=0` (no blocking at all), and the existing
`E3_SESH_CONTEXT_ENABLED=0` (no cue). State is one JSON file per session under
`~/.codex/sesh-gate/sessions` with the reason, turn id and count only.

Not covered: Codex `thread_spawn` subagents of a person's thread still get the
cue (they are delegated work, not templates). Before claiming the cap works in
production, verify one real Codex Desktop session that an MCP-tool `PreToolUse`
exit 2 actually stops the call.
