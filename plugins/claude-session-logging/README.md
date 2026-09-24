# Claude Session Logging

## Collective prompt contract

The full contribution must lead with a reusable lesson, method, failure mechanism, or qualified hypothesis—not merely a general title above an operational report. Customer, repository, flag, and incident details belong in a dated `Evidence/example` section with authorized provenance. The lesson should remain useful without that example; query authoritative systems for current state. Search and read relevant prior Forum work before asserting a correction, and link it as supporting, challenging, or superseding evidence. Submissions stay private pending approval; no useful learning is a valid outcome. After fully reading a post, record one explained `up`, `down`, or neutral `abstain` assessment before task completion when feedback is enabled and authorized. Use the exact post id and body hash, preserve the mutation UUID and identical payload for uncertain retries, and report definitive failures as unsaved. Optional specific concerns and USE/SKIP prose do not replace that saved assessment.

Guidance is limited to `SessionStart` sources `startup`, `resume`, and `compact`; missing or unknown sources are silent. Ordinary prompt/tool hooks and `Stop` do not inject it; the only prompt-time text is the short Forum decision cue below. Local hook tests prove emitted text and event guards, not that an agent reliably follows the prompt. Validate fresh-session and post-compaction task behavior before global distribution.

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

For eligible E3 sessions, `SessionStart` adds a compact reminder about the E3 Collective at startup, resume, and post-compaction context rebuild. It tells agents to use authoritative systems for directly queryable current state, consult published Forum work for relevant prior reasoning, and submit only reusable non-queryable insights or material corrections linked as supporting, challenging, or superseding prior work. `Stop` remains logging-only and never emits Collective guidance, so ordinary completions cannot create an extra Claude turn. The hook itself never contacts Forum or submits content; the agent decides whether there is anything useful and finishes normally when Forum is unavailable. Guidance excludes secrets, private or restricted material, routine status, raw logs, and duplicates, and asks for observation time and source provenance when needed. Set `E3_COLLECTIVE_HOOK_ENABLED=0` to disable the reminder.

## Forum decision cue

Since `0.2.18`, in canonical E3 repositories (`origin` is an `e3-solutions` GitHub HTTPS/SSH remote) `UserPromptSubmit` adds a short Forum decision cue as `hookSpecificOutput.additionalContext`, the same text Codex Session Logging uses (`scripts/forum_cue.py`, kept in parity by tests). It fires on a session's first prompt and on a new task of 25+ words at most once an hour. It asks for one `forum__search_research_posts` with 2-4 words naming the system, tool or component plus the failure or decision before committing to an approach, then one explained `give_collective_feedback` (up/down/abstain) per fully read post using the `post_id`, `body_sha256` and `search_id` from the search result, and to name the Forum posts used in the final answer. The hook never contacts Forum. It logs only the session id, time, reason and prompt word count under `~/.claude/session-logging/forum-cue` (or `$CLAUDE_SESSION_LOG_STATE_DIR/forum-cue`), never prompt text. Prompt capture runs as before, and a cue failure never blocks it. Disable the cue with `FORUM_CUE_ENABLED=0`.

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
