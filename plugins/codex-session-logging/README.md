# Codex Session Logging

Captures complete Codex parent and subagent activity through lifecycle hooks, including the exact native rollout JSONL bytes for messages, tool calls, tool outputs, reasoning records, and future record types.

The plugin treats Supabase Storage as the canonical location for full message/event payloads and Supabase Postgres as the queryable catalog. Hook scripts always spool locally first under `~/.codex/session-logging`, then start `scripts/drain_queue.py` in the background to POST queued records to the shared ingest endpoint.

At `SessionStart`, `UserPromptSubmit`, and `Stop`, the plugin reads Codex's native SQLite `threads` table to discover parent and subagent rollout paths, then captures only new bytes from those JSONL files. `PostToolUse` performs the same sweep only after agent-coordination tools such as spawn, wait, follow-up, or interrupt. Each immutable chunk is written to the durable local queue before its checkpoint advances. Complete JSONL lines are also inspected for Codex's cumulative token-count snapshots; every distinct fresh input, cached input, non-reasoning output, reasoning output, total, and context-window observation is queued. Codex's inclusive input/output counters are normalized into those additive components, and internally inconsistent snapshots are ignored. A repeated hook, network failure, concurrent hook, or crash therefore reuses deterministic identities without duplicating remote objects or regressing latest token totals. Even an unterminated crash tail is retained byte-for-byte, later appends continue at its exact offset, and file replacement starts a new generation. Parser state is bounded to a 1 MiB partial line. A per-database activity watermark avoids reopening unchanged historical rollouts on every hook.

There is no per-minute transcript poller and no external session process. A crash tail is recovered by the next lifecycle hook. The resident process remains responsible only for checking plugin updates every 30 minutes. Existing one-minute presence schedulers are removed automatically during upgrade.

Capture applies to every Codex task, including non-Git directories and Git checkouts without an `origin`. A real `origin` is retained when available; otherwise the client uses the fixed `codex://unscoped` catalog identity. Local workspace paths are not substituted for missing remotes.

Queryable hook event rows record only the tool name, phase, optional tool call id, and success flag when exposed by Codex. The private rollout objects retain the exact native JSONL, including tool arguments and outputs, so no available session data is discarded. Setup snapshots include sanitized Codex config names such as enabled plugins, installed skill names/sources, MCP server names/transport, marketplaces, app connection ids/tool names, and non-secret model/runtime settings.

Each runtime `session_id` is retained for event correlation. When Codex provides a `transcript_path`, the plugin also records a SHA-256 `thread_id` derived from that path so resumed runtime sessions can be grouped as one conversation without storing another copy of the path. Legacy records without a transcript reference remain separate runtime sessions and cannot be grouped reliably after the fact.

## Capture boundary and legacy compatibility

The first live hook considers native Codex tasks active during the previous 24 hours, prioritizes the current task family, and captures at most 8 MiB across 32 rollout files. Durable pending rows and byte offsets continue that bounded baseline on later hooks, including when SQLite exposes a row before its rollout file exists. After installation, a per-database watermark discovers every new or changed parent and subagent task without rescanning lifetime history.

Version 0.2.12 removes the disabled historical importer, retired resident-presence publisher, and repository-based admission filters from the client package and ingest function. Every Codex task can now deliver session records. This does not remove live messages, events, tool data, reasoning records, usage, environment snapshots, or unknown future rollout records. The ingest Edge Function continues to acknowledge and discard historical-backfill payloads from older queues, and the resident updater continues to remove old presence schedulers during upgrade.

## Supabase

Project: `codex-session-logging`

Project ref: `pmdfllwuctzkdjiehezq`

Default URL:

```bash
https://pmdfllwuctzkdjiehezq.supabase.co
```

Apply the SQL files in `supabase/migrations` when provisioning a new project. Chunk objects use the existing private bucket and their offsets, hashes, generations, and parent/root relationships are cataloged in `codex_session_events`. Immutable usage observations use the existing nine-argument `upsert_codex_session_usage_latest` RPC. It derives the immutable owner from `codex_sessions`, records every distinct observation, and advances the latest row only when its timestamp and all four cumulative token components do not move backwards. The Edge Function, RPC, and database constraint require the normalized token components to sum exactly to the reported total.

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

To recover usage from older rollout chunks, run the bounded admin replay in dry-run mode first. It reads the existing event catalog with a fixed cutoff and keyset cursor, defaults to the previous 72 hours, 500 sessions, and one session reader, verifies chunk offsets, sizes, and hashes, and makes no database writes unless `--apply` is passed. Use `--workers 4` for bounded session-level parallel reads. A resume must reuse the reported `cutoff` with `--cutoff` and the reported `resume_after_session` with `--after-session`. A later fresh run safely picks up concurrent catalog inserts; the monotonic RPC makes reruns idempotent.

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

Usage requests authenticate with the session's installation capability. Repository remotes and the `codex://unscoped` fallback are catalog identity, not authentication. `CODEX_SESSION_LOG_INGEST_TOKEN` is an optional additive gate for all requests; a shared or commercial deployment should configure it on the function and provision the same token to every client before relying on authenticated admission.

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
