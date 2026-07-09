# Claude Session Logging

Internal Claude Code plugin for Core Edge thread and tool metadata tracking.

The plugin captures Claude Code lifecycle hooks, spools event records locally under `~/.claude/session-logging`, and queues upload-ready event records for the shared session ingest endpoint. It records session/thread boundaries and tool names/phases only. It does not upload tool inputs, tool outputs, assistant message text, or full user prompt text.

Capture is scoped to repositories whose `origin` remote belongs to the `e3-solutions` GitHub organization. Other repositories return without writing local or remote session data.

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
```

## Drain

```bash
python3 plugins/claude-session-logging/scripts/drain_queue.py
```

Thread and tool metadata can still reveal sensitive work context. Keep the Supabase bucket private and read access constrained by existing RLS policies.
