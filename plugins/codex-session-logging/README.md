# Codex Session Logging

Captures Codex session activity through lifecycle hooks, including full user prompts, assistant responses, sanitized setup snapshots, and tool names.

The plugin treats Supabase Storage as the canonical location for full message/event payloads and Supabase Postgres as the queryable catalog. Hook scripts always spool locally first under `~/.codex/session-logging`, then start `scripts/drain_queue.py` in the background to POST queued records to the shared ingest endpoint.

Capture is scoped to repositories whose `origin` remote belongs to the `e3-solutions` GitHub organization. In other repositories, the hooks return without writing local or remote session data.

Tool logging records only the tool name, phase, optional tool call id, and success flag when exposed by Codex. Tool arguments, shell commands, and tool outputs are not uploaded. Setup snapshots include sanitized Codex config names such as enabled plugins, installed skill names/sources, MCP server names/transport, marketplaces, app connection ids/tool names, and non-secret model/runtime settings.

Each runtime `session_id` is retained for event correlation. When Codex provides a `transcript_path`, the plugin also records a SHA-256 `thread_id` derived from that path so resumed runtime sessions can be grouped as one conversation without storing another copy of the path. Legacy records without a transcript reference remain separate runtime sessions and cannot be grouped reliably after the fact.

## Historical backfill

Version 0.2.1 adds a resumable background importer for existing transcripts under `$CODEX_HOME/sessions` (normally `~/.codex/sessions`). `SessionStart` launches the importer without delaying Codex startup. It reads the repository URL recorded in each transcript, accepts only `e3-solutions` repositories, queues deterministic message records through the same ingest endpoint, and checkpoints progress under `~/.codex/session-logging/backfills/v1`.

Newer transcripts use canonical user-message events and final assistant answers so duplicated user envelopes and intermediate commentary are not imported. Older transcript formats fall back to user and assistant response messages. The importer also stores the final cumulative token-usage snapshot for each session in `codex_session_usage`, including input, cached-input, output, reasoning-output, and total tokens. Historical records retain their original timestamps and use deterministic IDs, making repeated and interrupted runs safe.

Supabase tracks rollout progress in `codex_session_backfill_runs`, keyed by user, installation, and backfill version. Running installations send throttled heartbeats during scanning and queue draining, followed by a final partial or complete status. This makes it possible to see which active installations have completed the update. Machines that have not started Codex since the release cannot report or upload until their next session.

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
export CODEX_SESSION_LOG_BACKFILL=0
export CODEX_SESSION_LOG_BACKFILL_MAX_FILES=1000
export CODEX_SESSION_LOG_BACKFILL_DRAIN_WAIT_SECONDS=7200
export CODEX_SESSION_LOG_BACKFILL_HEARTBEAT_SECONDS=30
```

Backfill is enabled by default. Set `CODEX_SESSION_LOG_BACKFILL=0` to disable it. When `CODEX_SESSION_LOG_AUTO_UPLOAD=0`, records remain queued locally, the run stays partial, and no completion status is sent to Supabase. Run or resume it manually with:

```bash
python3 plugins/codex-session-logging/scripts/backfill_sessions.py
```

## Drain

```bash
python3 plugins/codex-session-logging/scripts/drain_queue.py
```

Full prompts and assistant messages are sensitive. Keep the bucket private and use the Postgres RLS policies in the migration for user-owned reads.
