# Codex Session Logging

Captures full Codex user prompts and assistant responses through Codex lifecycle hooks.

The plugin treats Supabase Storage as the canonical location for full message payloads and Supabase Postgres as the queryable catalog. Hook scripts always spool locally first under `~/.codex/session-logging`, then start `scripts/drain_queue.py` in the background to POST queued records to the shared ingest endpoint.

Capture is scoped to repositories whose `origin` remote belongs to the `e3-solutions` GitHub organization. In other repositories, the hooks return without writing local or remote session data.

## Supabase

Project: `codex-session-logging`

Project ref: `pmdfllwuctzkdjiehezq`

Default URL:

```bash
https://pmdfllwuctzkdjiehezq.supabase.co
```

Apply the SQL files in `supabase/migrations` in order.

Deploy the ingest Edge Function from `supabase/functions/codex-session-ingest`. The function owns the Supabase admin key server-side and uses the developer's git email as the initial user key. If `CODEX_SESSION_LOG_USER_EMAIL_MAP` contains the email, that mapped Supabase Auth user id is used; otherwise the function derives a stable UUID from the email so sessions are tracked without per-user setup.

```bash
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
export CODEX_SESSION_LOG_INGEST_TOKEN=<only-if-the-function-requires-one>
```

## Drain

```bash
python3 plugins/codex-session-logging/scripts/drain_queue.py
```

Full prompts and assistant messages are sensitive. Keep the bucket private and use the Postgres RLS policies in the migration for user-owned reads.
