# Sesh (Claude Code plugin)

The Sesh hooks for Claude Code (COR-4888). Until now they existed only in Codex
(`plugins/sesh`, 0.1.0). Claude Code sessions reached Sesh only through Sherlock uploads, with no
cues and no record of Sesh tool use. Installed as `sesh@coreedge-internal`, next to
`claude-session-logging` or without it.

## What it does

| Hook | Behaviour | Switch (default) |
|---|---|---|
| `SessionStart` | Startup "Sesh prior-work context" cue in e3-solutions repositories (startup and `/clear`; not resume or compaction). Same text as Codex. | `E3_SESH_CONTEXT_ENABLED=0` removes it; `E3_SESH_CUE_AFTER_COMPACTION=1` adds the compaction cue |
| `SessionStart` | COR-4808 routine A/B: human sessions in arm `need_only` get no startup cue (same hash as Codex) | `E3_SESH_ROUTINE_AB` (on) |
| `UserPromptSubmit` | One-line cue to search Sesh once when a person asks about earlier work (same pattern as Codex) | `E3_SESH_PAST_WORK_CUE` (on) |
| `PreToolUse` | One Sesh coding-session search per turn; the second is denied. Ask, find-similar and open are never limited. | `E3_SESH_ONE_PER_TURN` (on) |
| `PreToolUse` on `Write` | COR-4887 plan cue: before a new service, migration/table, script or module file is created, one Sesh search for existing work, shown as an existing-work card (link, owner, date, what it did). At most twice a session, once per area, never for tests/docs/config, never in a turn that already searched. | `E3_SESH_PLAN_CUE` (on) |
| all cues | COR-4887 holdout: a stable 15% of sessions gets no cue at all (startup, past-work or plan). Tools stay. | `E3_SESH_HOLDOUT_PCT` (15; 0 turns it off) |
| `PreToolUse`/`PostToolUse`/`PostToolUseFailure` | Metadata-only linking of Sesh tool calls (below) | `E3_SESH_LINKING` (on) |

A turn is one message you send. Claude Code gives hooks no turn id, so `UserPromptSubmit` records
one. Sesh tools are matched under any MCP server name, including claude.ai connectors
(`mcp__<uuid>__sesh__search_coding_sessions`) and local servers (`mcp__cosmos__sesh__...`).

**Bots keep Sesh.** Subagents (`agent_id` in the hook payload) and SDK/headless sessions
(`CLAUDE_CODE_ENTRYPOINT=sdk-*`, or the transcript's `entrypoint`) are never limited, never held
out and never in the routine A/B. `E3_SESH_EXPERIMENT_OPTOUT=1` does the same for a person.
Every check fails open: an error never blocks a tool.

### Arms

Both are functions of the session id alone, so analysis recomputes them in SQL:

```sql
-- holdout (15%): no cue at all
(get_byte(d, 0) * 256 + get_byte(d, 1)) % 100 < 15
  where d = sha256(convert_to('sesh_holdout_v1:' || lower(btrim(session_id)), 'UTF8'))
-- routine A/B inside the treated 85%: 0 = routine, 1 = need_only
get_byte(sha256(convert_to('sesh_routine_v1:' || lower(btrim(session_id)), 'UTF8')), 0) % 2
```

Every event also carries `sesh_arm` (`holdout`, `treated`, `bot`, `optout`, `unassigned`),
`sesh_routine_arm`, `sesh_holdout_pct` and `sesh_plugin_version`.

## Search linking and who runs it

For Sesh tools only, the plugin queues metadata-only events to the same `codex-session-ingest`
function `claude-session-logging` uses (same identity, same `CLAUDE_SESSION_LOG_*` settings and
upload opt-out `CLAUDE_SESSION_LOG_AUTO_UPLOAD=0`), with `platform: "claude-code"`, `agent: "claude"`:

- `environment_snapshot` at SessionStart: `codex_setup.skills` names `search-coding-sessions`
  from plugin `sesh`, marketplace `coreedge-internal` and the version;
- `tool_call_started` / `tool_call_finished` / `tool_call_failed` for Sesh tools: tool name, call
  id, `sesh_request_id(s)` of a search (the search-to-session link), success, `duration_ms`,
  arm, version, and `sesh_turn_guard: denied` on a denied search;
- `sesh_plan_cue_shown` / `sesh_plan_cue_withheld` (holdout) when the plan cue fires: kind,
  topic hash and index only.

**Not sent:** prompts, assistant text, queries, passages, tool input or output, file names, and
any other tool. e3-solutions repositories only. The events use `seq` from 1,500,000,000 so they
never overwrite the logger's own event objects.

Today's ingest keeps `tool_name`, `tool_phase`, `sesh_request_id(s)`, `success`, the
`codex_setup.skills` entry and the event type. It drops the newer labels (arm, version, timing,
guard, plan-cue fields) until the separate ingest PR's `CODEX_SESSION_INGEST_SESH_TELEMETRY` flag
is turned on; the arms can be recomputed from the session id meanwhile.

Who runs it, and which version, with no server change:

```sql
select s.user_id, sk->>'version' as version, sk->>'marketplace' as marketplace,
       max(s.created_at) as last_seen, count(distinct s.session_id) as sessions
from codex_session_events s
cross join lateral jsonb_array_elements(s.metadata->'codex_setup'->'skills') sk
where s.event_type = 'environment_snapshot' and s.metadata->>'agent' = 'claude'
  and sk->>'plugin' = 'sesh'
group by 1, 2, 3 order by last_seen desc;
```

Linked Claude Code searches: `event_type = 'tool_call_finished' and metadata->>'agent' = 'claude'
and metadata ? 'sesh_request_id'`. Plan-cue fires: `event_type like 'sesh_plan_cue_%'`.

## Install

One command (Mac or Linux, Claude Code, Python 3, Git access to this repository):

```sh
gh api repos/e3-solutions/codex-plugins/contents/plugins/claude-sesh/install.py -H 'Accept: application/vnd.github.raw' | python3 - 
```

or, by hand: `claude plugin marketplace add e3-solutions/codex-plugins` (skip if you already have
`coreedge-internal`, then run `claude plugin marketplace update coreedge-internal`) and
`claude plugin install sesh@coreedge-internal`. Then start a new Claude Code session.

`install.py` is safe to rerun (it repairs a disabled or old install and otherwise changes nothing),
touches no other plugin, refuses if a `sesh` plugin from another marketplace is installed, and
honours `CLAUDE_CONFIG_DIR`. `--uninstall` removes only `sesh@coreedge-internal`. Updates arrive
with the marketplace (`claude plugin marketplace update coreedge-internal`, or Claude Code's
marketplace auto-update).

Rollback on a machine: `claude plugin disable sesh@coreedge-internal`, or turn single features off
with the switches above.
