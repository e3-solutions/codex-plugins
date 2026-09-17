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
