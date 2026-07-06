# Codex Session Logging

Captures full Codex user prompts and assistant responses through Codex lifecycle hooks.

The plugin treats Supabase Storage as the canonical location for full message payloads and Supabase Postgres as the queryable catalog. Hook scripts always spool locally first under `~/.codex/session-logging`, then start `scripts/drain_queue.py` in the background when Supabase credentials are configured.

Capture is scoped to repositories whose `origin` remote belongs to the `e3-solutions` GitHub organization. In other repositories, the hooks return without writing local or remote session data.

## Supabase

Project: `codex-session-logging`

Project ref: `pmdfllwuctzkdjiehezq`

Default URL:

```bash
https://pmdfllwuctzkdjiehezq.supabase.co
```

Apply the SQL files in `supabase/migrations` in order.

## Environment

```bash
export CODEX_SESSION_LOG_SUPABASE_URL=https://pmdfllwuctzkdjiehezq.supabase.co
export CODEX_SESSION_LOG_SUPABASE_SERVICE_ROLE_KEY=<service-role-key>
export CODEX_SESSION_LOG_USER_ID=<auth.users.id for this Codex user>
```

Optional:

```bash
export CODEX_SESSION_LOG_STATE_DIR=~/.codex/session-logging
export CODEX_SESSION_LOG_BUCKET=codex-sessions
```

## Drain

```bash
python3 plugins/codex-session-logging/scripts/drain_queue.py
```

Full prompts and assistant messages are sensitive. Keep the bucket private and use the Postgres RLS policies in the migration for user-owned reads.
