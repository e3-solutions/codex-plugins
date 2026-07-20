# Claude Session Logging

Internal Claude Code plugin for Core Edge thread and tool metadata tracking.

The plugin captures Claude Code lifecycle hooks, spools event records locally under `~/.claude/session-logging`, and queues upload-ready event records for the shared session ingest endpoint. It records session/thread boundaries and tool names/phases only. It does not upload tool inputs, tool outputs, assistant message text, or full user prompt text.

Capture is scoped to repositories whose `origin` remote belongs to the `e3-solutions` GitHub organization. Other repositories return without writing local or remote session data.

Every emitted payload is tagged `metadata.agent = "claude"` (Codex tags `"codex"`), so the shared ingest, heartbeat dashboard, and codestat can label and classify sessions by coding agent. This is a metadata-only label and carries no user content.

## Liveness

The plugin feeds the E3 heartbeat dashboard two ways:

- **Lifecycle events.** Each captured event upserts the session's `codex_sessions` row (id, fresh `updated_at`). `Stop` and `SessionEnd` additionally set `ended_at`; every other event (including presence) clears it, so a resumed session lights back up.
- **Idle-but-open presence.** Claude has no native session database, so the equivalent "session is open" signal is the transcript file `~/.claude/projects/<slug>/<sessionId>.jsonl` — its name is the session id and each turn appends a line, so its mtime tracks activity. `SessionStart` spawns a detached ~60s **ticker** (`presence_ticker.py`) that republishes metadata-only presence (`scripts/publish_presence.py`) while the transcript stays fresh and publishes a final `ended` presence after ~5 minutes idle. Both reuse the plugin's existing ingest queue. Presence reads only the session id, cwd, repo/branch, and activity timestamp — never prompts, responses, tool calls, or transcript bodies.

Set `CLAUDE_SESSION_LOG_PRESENCE=0` to disable the presence ticker.

## Captured Events

- `SessionStart` as `thread_started`
- `UserPromptSubmit` as `thread_prompt_submitted`, with prompt byte size and SHA-256 hash only
- `Stop` as `thread_stopped`
- `StopFailure` as `thread_stop_failed`
- `PreCompact` and `PostCompact` as compaction lifecycle events
- `SessionEnd` as `thread_ended`
- `PreToolUse`, `PostToolUse`, and `PostToolUseFailure` as tool call lifecycle events
- `PermissionRequest` and `PermissionDenied` as tool permission lifecycle events
- `PostToolBatch` as a batch count event

## Internal Installation

Add the internal marketplace from this repository, then install the plugin from Claude Code:

```bash
/plugin marketplace add git@github.com:e3-solutions/codex-plugins.git
/plugin install claude-session-logging@coreedge-internal
```

The plugin checks the internal marketplace once per day on `SessionStart` and updates the installed plugin in the background. Updates take effect in the next Claude Code session. Set `CLAUDE_SESSION_LOG_AUTO_UPDATE=0` to disable this, or set `CLAUDE_SESSION_LOG_AUTO_UPDATE_INTERVAL_SECONDS` to change the interval.

For local development from a checkout:

```bash
claude --plugin-dir ./plugins/claude-session-logging
```

## Environment

No local environment variables are required for normal installs.

Optional:

```bash
export CLAUDE_SESSION_LOG_STATE_DIR=~/.claude/session-logging
export CLAUDE_SESSION_LOG_BUCKET=codex-sessions
export CLAUDE_SESSION_LOG_INGEST_URL=https://pmdfllwuctzkdjiehezq.supabase.co/functions/v1/codex-session-ingest
export CLAUDE_SESSION_LOG_AUTO_UPLOAD=0
export CLAUDE_SESSION_LOG_AUTO_UPDATE=0
export CLAUDE_SESSION_LOG_AUTO_UPDATE_INTERVAL_SECONDS=86400
export CLAUDE_SESSION_LOG_INGEST_TOKEN=<only-if-the-function-requires-one>
export CLAUDE_SESSION_LOG_PRESENCE=0            # disable the idle-but-open presence ticker
export CLAUDE_SESSION_LOG_BACKFILL=0            # disable session history backfill
```

## Backfill

`scripts/backfill_sessions.py` idempotently replays recent local transcripts
(`~/.claude/projects/*/*.jsonl`, last 48h by default, e3-solutions repos only)
and re-emits metadata-only `thread_started` / `thread_ended` events tagged
`agent=claude` and `source=historical_transcript` through the same queue, so 24h
of session history is available on install.

```bash
python3 plugins/claude-session-logging/scripts/backfill_sessions.py            # replay + upload
python3 plugins/claude-session-logging/scripts/backfill_sessions.py --dry-run  # count only
```

Env: `CLAUDE_SESSION_LOG_BACKFILL=0` disables it; `CLAUDE_SESSION_LOG_BACKFILL_HOURS`
(default 48) and `CLAUDE_SESSION_LOG_BACKFILL_MAX_FILES` (default 1000) bound it.

NOTE: the shared ingest currently drops `source=historical_transcript` records
without writes (identical to Codex — see `isHistoricalBackfill` in the ingest
function). The backfill script is parity-complete and safe to run today, but its
records only land once historical ingestion is enabled server-side.

## Drain

```bash
python3 plugins/claude-session-logging/scripts/drain_queue.py
```

Thread and tool metadata can still reveal sensitive work context. Keep the Supabase bucket private and read access constrained by existing RLS policies.
