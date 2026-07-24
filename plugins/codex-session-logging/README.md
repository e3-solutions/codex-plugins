# Codex Session Logging

Captures complete Codex parent and subagent activity through lifecycle hooks, including the exact native rollout JSONL bytes for messages, tool calls, tool outputs, reasoning records, and future record types.

The plugin treats Supabase Storage as the canonical location for full message/event payloads and Supabase Postgres as the queryable catalog. Hook scripts always spool locally first under `~/.codex/session-logging`, then start `scripts/drain_queue.py` in the background to POST queued records to the shared ingest endpoint.

At `SessionStart`, `UserPromptSubmit`, and `Stop`, the plugin reads Codex's native SQLite `threads` table to discover parent and subagent rollout paths, then captures only new bytes from those JSONL files. `PostToolUse` performs the same sweep only after agent-coordination tools such as spawn, wait, follow-up, or interrupt. Each immutable chunk is written to the durable local queue before its checkpoint advances. A repeated hook, network failure, concurrent hook, or crash therefore reuses the same deterministic identity without duplicating remote objects or rows. Even an unterminated crash tail is retained byte-for-byte, later appends continue at its exact offset, and file replacement starts a new generation. A per-database activity watermark avoids reopening unchanged historical rollouts on every hook.

There is no per-minute transcript poller and no external session process. A crash tail is recovered by the next lifecycle hook. The resident process remains responsible only for checking plugin updates every 30 minutes. Existing one-minute presence schedulers are removed automatically during upgrade.

Capture is scoped to repositories whose `origin` remote belongs to the `e3-solutions` GitHub organization. In other repositories, the hooks return without writing local or remote session data.

Queryable hook event rows record only the tool name, phase, optional tool call id, and success flag when exposed by Codex. The private rollout objects retain the exact native JSONL, including tool arguments and outputs, so no available session data is discarded. Setup snapshots include sanitized Codex config names such as enabled plugins, installed skill names/sources, MCP server names/transport, marketplaces, app connection ids/tool names, and non-secret model/runtime settings.

Each runtime `session_id` is retained for event correlation. When Codex provides a `transcript_path`, the plugin also records a SHA-256 `thread_id` derived from that path so resumed runtime sessions can be grouped as one conversation without storing another copy of the path. Legacy records without a transcript reference remain separate runtime sessions and cannot be grouped reliably after the fact.

## Legacy historical backfill

Historical transcript backfills are disabled as of version 0.2.2. `SessionStart` captures only the live environment snapshot and no longer launches `backfill_sessions.py`. The ingest Edge Function acknowledges and discards historical-backfill data and status payloads so queues created by older plugin versions can drain without modifying Storage or Postgres.

The legacy importer and its checkpoint files remain in the repository only for auditability. Do not run it; historical analysis uses the separately imported `ai_session_*` archive tables.

The 0.2.7 rollout synchronizer does not parse or reconstruct legacy transcript messages. Its first hook considers only native E3 tasks active during the previous 24 hours, prioritizes the current task family, and captures at most 8 MiB across 32 rollout files before returning. Durable pending rows and byte offsets continue that bounded baseline on later hooks, including when SQLite exposes a row before its rollout file exists. Older history remains under the legacy archive policy instead of being auto-uploaded during upgrade. After installation, the SQLite watermark discovers new or changed parent and subagent tasks without rescanning lifetime history.

## Supabase

Project: `codex-session-logging`

Project ref: `pmdfllwuctzkdjiehezq`

Default URL:

```bash
https://pmdfllwuctzkdjiehezq.supabase.co
```

Apply the SQL files in `supabase/migrations` when provisioning a new project. Complete rollout capture itself requires no new database migration: chunk objects use the existing private bucket and their offsets, hashes, generations, and parent/root relationships are cataloged in `codex_session_events`.

Deploy the ingest Edge Function from `supabase/functions/codex-session-ingest` after the migration. The function owns the Supabase admin key server-side and uses the developer's git email as the initial user key when available. If `CODEX_SESSION_LOG_USER_EMAIL_MAP` contains the email, that mapped Supabase Auth user id is used; otherwise the function derives a stable UUID from the email. When git email is not configured, the plugin sends a persistent local installation id so sessions still track without per-user setup.

```bash
supabase db push --project-ref pmdfllwuctzkdjiehezq
supabase functions deploy codex-session-ingest --project-ref pmdfllwuctzkdjiehezq
supabase secrets set \
  --project-ref pmdfllwuctzkdjiehezq \
  CODEX_SESSION_LOG_USER_EMAIL_MAP='{"user@e3.solutions":"00000000-0000-0000-0000-000000000000"}'
```

The email map is optional for the first rollout and can be added later to merge deterministic ids into real Auth users. The function reads `SUPABASE_SECRET_KEYS` by default, with `SUPABASE_SERVICE_ROLE_KEY` as a legacy fallback. Do not put either key on developer machines or in the plugin package.

## Environment

No local environment variables are required for normal installs.

Optional:

```bash
export CODEX_SESSION_LOG_STATE_DIR=~/.codex/session-logging
export CODEX_SESSION_LOG_BUCKET=codex-sessions
export CODEX_SESSION_LOG_INGEST_URL=https://pmdfllwuctzkdjiehezq.supabase.co/functions/v1/codex-session-ingest
export CODEX_SESSION_LOG_AUTO_UPLOAD=0
export CODEX_SESSION_LOG_UPLOAD_WORKERS=4
export CODEX_SESSION_LOG_INGEST_TOKEN=<only-if-the-function-requires-one>
```

An explicit `CODEX_SESSION_LOG_AUTO_UPLOAD=0` or `=1` is persisted in
`~/.codex/session-logging/preferences.json`, independent of a custom queue directory. A queue is
never interpreted as consent state because enabled clients also queue records during ordinary
outages. The choice is persisted the next time 0.2.7 observes the explicit variable. Set the
variable to `1` once in Codex to re-enable uploads.

## Drain

```bash
python3 plugins/codex-session-logging/scripts/drain_queue.py
```

Full prompts and assistant messages are sensitive. Keep the bucket private and use the Postgres RLS policies in the migration for user-owned reads.
