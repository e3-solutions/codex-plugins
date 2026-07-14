# Codex Session Logging

Captures Codex session activity through lifecycle hooks, including full user prompts, assistant responses, sanitized setup snapshots, and tool names.

The plugin treats Supabase Storage as the canonical location for full message/event payloads and Supabase Postgres as the queryable catalog. Hook scripts always spool locally first under `~/.codex/session-logging`, then start `scripts/drain_queue.py` in the background to POST queued records to the shared ingest endpoint.

Task presence does not depend on Codex reloading hooks. The Core Edge resident updater installs a separate macOS LaunchAgent that reads only task identity, repository, branch, rollout path, and native activity timestamps from Codex's local `threads` table once per minute. It publishes one deterministic `resident_presence` event per task and replaces that event when activity advances. It never reads task titles, previews, prompts, responses, or transcript contents. Hook-based prompt, response, and tool capture continues independently whenever the hooks are active.

Capture is scoped to repositories whose `origin` remote belongs to the `e3-solutions` GitHub organization. In other repositories, the hooks return without writing local or remote session data.

Tool logging records only the tool name, phase, optional tool call id, and success flag when exposed by Codex. Tool arguments, shell commands, and tool outputs are not uploaded. Setup snapshots include sanitized Codex config names such as enabled plugins, installed skill names/sources, MCP server names/transport, marketplaces, app connection ids/tool names, and non-secret model/runtime settings.

Each runtime `session_id` is retained for event correlation. When Codex provides a `transcript_path`, the plugin also records a SHA-256 `thread_id` derived from that path so resumed runtime sessions can be grouped as one conversation without storing another copy of the path. Legacy records without a transcript reference remain separate runtime sessions and cannot be grouped reliably after the fact.

## Historical backfill

Historical transcript backfills are disabled as of version 0.2.2. `SessionStart` captures only the live environment snapshot and no longer launches `backfill_sessions.py`. The ingest Edge Function acknowledges and discards historical-backfill data and status payloads so queues created by older plugin versions can drain without modifying Storage or Postgres.

The legacy importer and its checkpoint files remain in the repository only for auditability. Do not run it; historical analysis uses the separately imported `ai_session_*` archive tables.

## Supabase

Project: `codex-session-logging`

Project ref: `pmdfllwuctzkdjiehezq`

Default URL:

```bash
https://pmdfllwuctzkdjiehezq.supabase.co
```

Apply the SQL files in `supabase/migrations` before deploying the ingest Edge Function. The thread identity migration includes a compatibility trigger, so the previously deployed function continues to create sessions during this rollout. Deploying the function first is unsupported because it queries the new `thread_id` column.

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
`~/.codex/session-logging/preferences.json`, independent of a custom queue directory, so the
resident publisher honors the same choice even though macOS LaunchAgents do not inherit shell
environment variables. A queue is never interpreted as consent state because enabled clients also
queue records during ordinary outages. Environment-only 0.2.2 choices leave no durable state for a
separate LaunchAgent to inspect; that legacy limitation cannot be reconstructed during upgrade, so
the choice is persisted the next time 0.2.3 observes the explicit variable. Set the variable to `1`
once in Codex to re-enable uploads.

## Drain

```bash
python3 plugins/codex-session-logging/scripts/drain_queue.py
```

Full prompts and assistant messages are sensitive. Keep the bucket private and use the Postgres RLS policies in the migration for user-owned reads.
