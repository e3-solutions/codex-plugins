# Codex Session Logging

## Sesh receipt correlation

The metadata-only `PostToolUse` event retains `sesh_request_id` only for the exact
Sesh search tool under the observed exact connector prefixes `mcp__e3_cosmos__`,
`mcp__e3__`, `mcp__cosmos__`, or `mcp__cosmos_e3__`, each followed by
`sesh__search_coding_sessions`. Other names and suffix variants are rejected.
It extracts the canonical UUID
from a direct response, `structuredContent`, or one JSON text-content block.
Conflicting, malformed, duplicate-key, or oversized text envelopes omit this
optional field. They do not block the tool or change the base event.

The ingest sanitizer accepts this field only on that tool's finished event with
phase `finished`. No query, passage, or raw tool response is copied into event
metadata. Existing rollout capture and privacy controls are unchanged. The ID is
client-attested correlation, not authorization, provenance, relevance, successful
source opening, or proof that the evidence helped. Older clients/events lack it;
do not infer missing usage or usefulness from a missing link.

Finished search events may also retain an ordered, deduplicated list of at most
75 SHA-256 digests for the exact bounded source handles present in the returned
result/evidence envelopes. Finished `open_coding_session_source` and
`timetracker__get_chat` events may retain the matching opened-handle digest and
a `verified`, `failed`, or `unknown` witness. `verified` is limited to an exact
Sesh source response whose reference/message IDs agree and whose verification is
`exact_source_bytes`; Cosmos `get_chat` remains `unknown` absent a separately
specified strict witness. Digests use canonical public tool arguments and never
retain the raw reference/session ID, query, passage, source body, or arbitrary
tool input/output. Malformed, conflicting, oversized, duplicate, or unsupported
envelopes omit the optional metadata without changing tool behavior.

Deploy the compatible sanitizer before distributing this logging release. Rollback of
either side safely omits the optional field; no schema migration is required.

## Sesh cue: startup only (COR-4688)

The `SessionStart` hook prints the "Sesh prior-work context" cue at `startup` in
e3-solutions repositories. It asks for one Sesh search using the user's actual task,
then opening the top result with the exact returned source arguments before the first
edit. Since 0.2.35 the cue is **not** shown after compaction, for any thread (bots
included): in COR-4681 the post-compaction cue drove 29% of Codex Sesh searches, only
13% were opened, and about 2.7% helped. Set `E3_SESH_CUE_AFTER_COMPACTION=1` (also
`true` or `on`) to bring back the previous cue at compaction. `E3_SESH_CONTEXT_ENABLED=0`
still turns the cue off everywhere.

## Sesh startup-cue experiment (COR-4681, off by default)

This release adds an on/off experiment for the startup cue. **It ships off and stays off until it is announced to the team.** With the
flag unset the hook output and the logged event are exactly as before.

- `E3_SESH_CUE_EXPERIMENT=1` (also `true` or `on`; anything else is off) turns it on.
- Arms: each session gets `cue` (startup cue shown) or `no_cue` (cue not shown),
  50/50 by `sha256(<ISO week> + ":" + <session id>)`. The week (for example
  `2026-W42`) comes from the time inside the Codex UUIDv7 session id, so a session
  keeps its arm after compaction even across a week boundary; non-UUIDv7 ids use the
  current week. `E3_SESH_CUE_EXPERIMENT_SALT=YYYY-Www` overrides the week (testing only).
- Opt-out: `E3_SESH_EXPERIMENT_OPTOUT=1` always shows the cue; arm `optout`.
- Bots keep Sesh: automated threads always get the cue; arm `bot`. A thread is
  automated when its rollout header `thread_source` is anything other than `user`
  (`agent_created_thread`, `guardian_review`, `subagent`, ...), its `source` is a
  subagent or `exec`, or its originator is `codex_exec`.
- If the thread or session id cannot be read, the cue is shown; arm `unassigned`.
- The existing kill switch still wins: with `E3_SESH_CONTEXT_ENABLED=0` (or
  `E3_SESH_START_SEARCH_ENABLED=0`) there is no cue; arm `disabled`.

While the flag is on, the `SessionStart` `environment_snapshot` event carries
`metadata.sesh_cue_experiment = "sesh_cue_v1"`, `metadata.sesh_cue_arm` (one of the
arms above) and `metadata.sesh_cue_salt` (the week), at startup and after compaction
(after compaction no cue is shown in either arm unless `E3_SESH_CUE_AFTER_COMPACTION` is on). Nothing else is added; no
question, prompt or chat text is read or logged. The local copy is in
`events.jsonl` and the queued event file under the logger state directory. The ingest
sanitizer keeps the three keys only when all are valid and both copies agree. **Deploy
the updated `codex-session-ingest` function before turning the flag on**, or the
arms are dropped and never reach `public.codex_session_events`.

## Collective prompt contract

The full contribution must lead with a reusable lesson, method, failure mechanism, or qualified hypothesis—not merely a general title above an operational report. Customer, repository, flag, and incident details belong in a dated `Evidence/example` section with authorized provenance. The lesson should remain useful without that example; query authoritative systems for current state. Search and read relevant prior Forum work before asserting a correction, and link it as supporting, challenging, or superseding evidence. Submissions stay private pending approval; no useful learning is a valid outcome. After fully reading a post, record one explained `up`, `down`, or neutral `abstain` assessment before task completion when feedback is enabled and authorized. Use the exact post id and body hash, preserve the mutation UUID and identical payload for uncertain retries, and report definitive failures as unsaved. Optional specific concerns and USE/SKIP prose do not replace that saved assessment.

Guidance is limited to `SessionStart` sources `startup`, `resume`, and `compact`; missing or unknown sources are silent. Ordinary prompt/tool hooks and `Stop` do not inject it. Local hook tests prove emitted text and event guards, not that an agent reliably follows the prompt. Validate fresh-session and post-compaction task behavior before global distribution.

Captures complete Codex parent and subagent activity through lifecycle hooks, including the exact native rollout JSONL bytes for messages, tool calls, tool outputs, reasoning records, and future record types.

The plugin treats Supabase Storage as the canonical location for full message/event payloads and Supabase Postgres as the queryable catalog. Hook scripts always spool locally first under `~/.codex/session-logging`, then start `scripts/drain_queue.py` in the background to POST queued records to the shared ingest endpoint.

At `SessionStart`, `UserPromptSubmit`, and `Stop`, the plugin reads Codex's native SQLite `threads` table to discover parent and subagent rollout paths, then captures only new bytes from those JSONL files. `PostToolUse` performs the same sweep only after agent-coordination tools such as spawn, wait, follow-up, or interrupt. Each immutable chunk is written to the durable local queue before its checkpoint advances. Complete JSONL lines are also inspected for Codex's cumulative token-count snapshots; every distinct fresh input, cached input, non-reasoning output, reasoning output, total, and context-window observation is queued. Codex's inclusive input/output counters are normalized into those additive components, and internally inconsistent snapshots are ignored. A repeated hook, network failure, concurrent hook, or crash therefore reuses deterministic identities without duplicating remote objects or regressing latest token totals. Even an unterminated crash tail is retained byte-for-byte, later appends continue at its exact offset, and file replacement starts a new generation. Parser state is bounded to a 1 MiB partial line. A per-database activity watermark avoids reopening unchanged historical rollouts on every hook.

There is no per-minute transcript poller and no external session process. A crash tail is recovered by the next lifecycle hook. The resident process remains responsible only for checking plugin updates every 30 minutes. Existing one-minute presence schedulers are removed automatically during upgrade.

Capture is scoped to repositories whose `origin` remote belongs to the `e3-solutions` GitHub organization. In other repositories, the hooks return without writing local or remote session data.

For eligible E3 sessions, `SessionStart` adds a compact reminder about the E3 Collective at startup, resume, and post-compaction context rebuild. It tells agents to use authoritative systems for directly queryable current state, consult published Forum work for relevant prior reasoning, and submit only reusable non-queryable insights or material corrections linked as supporting, challenging, or superseding prior work. `Stop` remains logging-only and never emits Collective guidance, so ordinary completions cannot create an extra agent turn. The hook itself never contacts Forum or submits content; the agent decides whether there is anything useful and finishes normally when Forum is unavailable. Guidance excludes secrets, private or restricted material, routine status, raw logs, and duplicates, and asks for observation time and source provenance when needed. Set `E3_COLLECTIVE_HOOK_ENABLED=0` to disable the reminder.

Queryable hook event rows record only the tool name, phase, optional tool call id, and success flag when exposed by Codex. The private rollout objects retain the exact native JSONL, including tool arguments and outputs, so no available session data is discarded. Setup snapshots include sanitized Codex config names such as enabled plugins, installed skill names/sources, MCP server names/transport, marketplaces, app connection ids/tool names, and non-secret model/runtime settings.

Each runtime `session_id` is retained for event correlation. When Codex provides a `transcript_path`, the plugin also records a SHA-256 `thread_id` derived from that path so resumed runtime sessions can be grouped as one conversation without storing another copy of the path. Legacy records without a transcript reference remain separate runtime sessions and cannot be grouped reliably after the fact.

## Legacy historical backfill

Historical transcript backfills are disabled as of version 0.2.2. `SessionStart` captures only the live environment snapshot and no longer launches `backfill_sessions.py`. The ingest Edge Function acknowledges and discards historical-backfill data and status payloads so queues created by older plugin versions can drain without modifying Storage or Postgres.

The legacy importer and its checkpoint files remain in the repository only for auditability. Do not run it; historical analysis uses the separately imported `ai_session_*` archive tables.

The 0.2.8 rollout synchronizer does not parse or reconstruct legacy transcript messages. It only extracts cumulative usage while capturing native rollouts already admitted by the live 24-hour discovery policy. Its first hook considers only native E3 tasks active during the previous 24 hours, prioritizes the current task family, and captures at most 8 MiB across 32 rollout files before returning. Durable pending rows and byte offsets continue that bounded baseline on later hooks, including when SQLite exposes a row before its rollout file exists. Older history remains under the legacy archive policy instead of being auto-uploaded during upgrade. After installation, the SQLite watermark discovers new or changed parent and subagent tasks without rescanning lifetime history.

## Supabase

Project: `codex-session-logging`

Project ref: `pmdfllwuctzkdjiehezq`

Default URL:

```bash
https://pmdfllwuctzkdjiehezq.supabase.co
```

Apply the SQL files in `supabase/migrations` when provisioning a new project. Chunk objects use the existing private bucket and their offsets, hashes, generations, and parent/root relationships are cataloged in `codex_session_events`. Version 0.2.11 adds immutable usage observations to the existing nine-argument `upsert_codex_session_usage_latest` RPC. It derives the immutable owner from `codex_sessions`, records every distinct observation, and advances the latest row only when its timestamp and all four cumulative token components do not move backwards. Deploy that migration before updating clients. The Edge Function, RPC, and database constraint require the normalized token components to sum exactly to the reported total.

Deploy the ingest Edge Function from `supabase/functions/codex-session-ingest` after migrations when provisioning a new project. The function owns the Supabase admin key server-side and uses the developer's git email as the initial user key when available. If `CODEX_SESSION_LOG_USER_EMAIL_MAP` contains the email, that mapped Supabase Auth user id is used; otherwise the function derives a stable UUID from the email. When git email is not configured, the plugin sends a persistent local installation id so sessions still track without per-user setup.

The ignore-session migration adds a private, service-role-only fenced purge. The
ingest function durably reserves its exact bucket/prefix before uploading, so a
failed cleanup remains discoverable. Purge completion clears those locators and
leaves only the task-id hash. Later writes are acknowledged without being stored;
the service role has no direct table-delete grant. The migration does not scan or
populate locators for historical tasks; each newly received task record reserves
its locator before its object can be uploaded.

A new canonical user or assistant message containing the exact, case-sensitive
substring `--ignore-extension` is ownership-checked and fenced before its content
is uploaded or cataloged. The fence updates only the existing session timestamp
and a transient boolean discovery flag; it never stores the trigger message.
Heartbeat consumes that flag, deletes its local projection, and drives the
bounded source purge. Spelling and case variants, non-message records, and
historical rows do not trigger this behavior.

For the first production rollout of the ignore-session protocol, deploy this Edge
Function first, wait at least ten minutes for older hosted invocations to drain,
then apply the ignore-session migration. During that interval the new function
fails before upload because its reservation RPC is not present; this is intentional.
Deploy Heartbeat only after both source steps succeed.

```bash
supabase functions deploy codex-session-ingest --project-ref pmdfllwuctzkdjiehezq
sleep 600
supabase db push --project-ref pmdfllwuctzkdjiehezq
```

Configure secrets separately as needed:

```bash
supabase secrets set \
  --project-ref pmdfllwuctzkdjiehezq \
  CODEX_SESSION_LOG_USER_EMAIL_MAP='{"user@e3.solutions":"00000000-0000-0000-0000-000000000000"}'
```

To recover usage from rollout chunks stored before 0.2.11, run the bounded admin replay in dry-run mode first. It reads the existing event catalog with a fixed cutoff and keyset cursor, defaults to the previous 72 hours, 500 sessions, and one session reader, verifies chunk offsets, sizes, and hashes, and makes no database writes unless `--apply` is passed. Use `--workers 4` for bounded session-level parallel reads. A resume must reuse the reported `cutoff` with `--cutoff` and the reported `resume_after_session` with `--after-session`. A later fresh run safely picks up concurrent catalog inserts; the monotonic RPC makes reruns idempotent.

```bash
SUPABASE_URL=https://pmdfllwuctzkdjiehezq.supabase.co \
SUPABASE_SERVICE_ROLE_KEY=... \
python3 plugins/codex-session-logging/supabase/scripts/reprocess_rollout_usage.py --workers 4

SUPABASE_URL=https://pmdfllwuctzkdjiehezq.supabase.co \
SUPABASE_SERVICE_ROLE_KEY=... \
python3 plugins/codex-session-logging/supabase/scripts/reprocess_rollout_usage.py --workers 4 --apply
```

The opt-in local database check creates a disposable database, applies the migration, and verifies grants, owner conflicts, and concurrent monotonic updates:

```bash
CODEX_SESSION_LOG_TEST_DATABASE_URL=postgresql://postgres:postgres@127.0.0.1:54322/postgres \
python3 plugins/codex-session-logging/supabase/scripts/test_usage_rpc_db.py
```

The email map is optional for the first rollout and can be added later to merge deterministic ids into real Auth users. The function reads `SUPABASE_SECRET_KEYS` by default, with `SUPABASE_SERVICE_ROLE_KEY` as a legacy fallback. Do not put either key on developer machines or in the plugin package.

## Environment

Usage requests authenticate with the session's installation capability. `CODEX_SESSION_LOG_INGEST_TOKEN` is an optional additive gate for all requests; configure it only when every client has also been provisioned with that token.

Optional:

```bash
export CODEX_SESSION_LOG_STATE_DIR=~/.codex/session-logging
export CODEX_SESSION_LOG_BUCKET=codex-sessions
export CODEX_SESSION_LOG_INGEST_URL=https://pmdfllwuctzkdjiehezq.supabase.co/functions/v1/codex-session-ingest
export CODEX_SESSION_LOG_AUTO_UPLOAD=0
export CODEX_SESSION_LOG_UPLOAD_WORKERS=4
```

An explicit `CODEX_SESSION_LOG_AUTO_UPLOAD=0` or `=1` is persisted in
`~/.codex/session-logging/preferences.json`, independent of a custom queue directory. A queue is
never interpreted as consent state because enabled clients also queue records during ordinary
outages. The choice is persisted the next time 0.2.8 observes the explicit variable. Set the
variable to `1` once in Codex to re-enable uploads.

## Drain

```bash
python3 plugins/codex-session-logging/scripts/drain_queue.py
```

Full prompts and assistant messages are sensitive. Keep the bucket private and use the Postgres RLS policies in the migration for user-owned reads.
