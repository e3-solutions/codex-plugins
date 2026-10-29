# Codex Session Logging

## Sesh receipt correlation

The metadata-only `PostToolUse` event retains `sesh_request_id` only for the exact
Sesh search tool under the observed exact connector prefixes `mcp__e3_cosmos__`,
`mcp__e3__`, `mcp__cosmos__`, or `mcp__cosmos_e3__`, each followed by
`sesh__search_coding_sessions`. Other names and suffix variants are rejected.
It extracts the canonical UUID
from a direct response, `structuredContent`, or one JSON text-content block.
Conflicting, malformed, duplicate-key, or oversized text envelopes omit this
optional field. They do not block the tool or change the base event.

The ingest sanitizer accepts this field only on that tool's finished event with
phase `finished`. No query, passage, or raw tool response is copied into event
metadata. Existing rollout capture and privacy controls are unchanged. The ID is
client-attested correlation, not authorization, provenance, relevance, successful
source opening, or proof that the evidence helped. Older clients/events lack it;
do not infer missing usage or usefulness from a missing link.

Finished search events may also retain an ordered, deduplicated list of at most
75 SHA-256 digests for the exact bounded source handles present in the returned
result/evidence envelopes. Finished `open_coding_session_source` and
`timetracker__get_chat` events may retain the matching opened-handle digest and
a `verified`, `failed`, or `unknown` witness. `verified` is limited to an exact
Sesh source response whose reference/message IDs agree and whose verification is
`exact_source_bytes`; Cosmos `get_chat` remains `unknown` absent a separately
specified strict witness. Digests use canonical public tool arguments and never
retain the raw reference/session ID, query, passage, source body, or arbitrary
tool input/output. Malformed, conflicting, oversized, duplicate, or unsupported
envelopes omit the optional metadata without changing tool behavior.

Deploy the compatible sanitizer before distributing this logging release. Rollback of
either side safely omits the optional field; no schema migration is required.

## Sesh cue: startup only (COR-4688)

The `SessionStart` hook prints the "Sesh prior-work context" cue at `startup` in
e3-solutions repositories. It asks for one Sesh search using the user's actual task,
then opening the top result with the exact returned source arguments before the first
edit. Since 0.2.35 the cue is **not** shown after compaction, for any thread (bots
included): in COR-4681 the post-compaction cue drove 29% of Codex Sesh searches, only
13% were opened, and about 2.7% helped. Set `E3_SESH_CUE_AFTER_COMPACTION=1` (also
`true` or `on`) to bring back the previous cue at compaction. `E3_SESH_CONTEXT_ENABLED=0`
still turns the cue off everywhere.

## Sesh startup-cue experiment (COR-4681, off by default)

This release adds an on/off experiment for the startup cue. **It ships off and stays off until it is announced to the team.** With the
flag unset the hook output and the logged event are exactly as before.

- `E3_SESH_CUE_EXPERIMENT=1` (also `true` or `on`; anything else is off) turns it on.
- Arms: each session gets `cue` (startup cue shown) or `no_cue` (cue not shown),
  50/50 by `sha256(<ISO week> + ":" + <session id>)`. The week (for example
  `2026-W42`) comes from the time inside the Codex UUIDv7 session id, so a session
  keeps its arm after compaction even across a week boundary; non-UUIDv7 ids use the
  current week. `E3_SESH_CUE_EXPERIMENT_SALT=YYYY-Www` overrides the week (testing only).
- Opt-out: `E3_SESH_EXPERIMENT_OPTOUT=1` always shows the cue; arm `optout`.
- Bots keep Sesh: automated threads always get the cue; arm `bot`. A thread is
  automated when its rollout header `thread_source` is anything other than `user`
  (`agent_created_thread`, `guardian_review`, `subagent`, ...), its `source` is a
  subagent or `exec`, or its originator is `codex_exec`.
- If the thread or session id cannot be read, the cue is shown; arm `unassigned`.
- The existing kill switch still wins: with `E3_SESH_CONTEXT_ENABLED=0` (or
  `E3_SESH_START_SEARCH_ENABLED=0`) there is no cue; arm `disabled`.

While the flag is on, the `SessionStart` `environment_snapshot` event carries
`metadata.sesh_cue_experiment = "sesh_cue_v1"`, `metadata.sesh_cue_arm` (one of the
arms above) and `metadata.sesh_cue_salt` (the week), at startup and after compaction
(after compaction no cue is shown in either arm unless `E3_SESH_CUE_AFTER_COMPACTION` is on). Nothing else is added; no
question, prompt or chat text is read or logged. The local copy is in
`events.jsonl` and the queued event file under the logger state directory. The ingest
sanitizer keeps the three keys only when all are valid and both copies agree. **Deploy
the updated `codex-session-ingest` function before turning the flag on**, or the
arms are dropped and never reach `public.codex_session_events`.

## Forum cues off by default (COR-5034, 0.2.44)

Forum is hidden from Cosmos since Oct 28, 2026 (e3-solutions/e3-mcp#86), so the plugin no longer tells agents to search, share or rate Forum posts unless a flag turns it back on (set it where the agent is launched; the code is kept):

| Cue | Flag (now default off) | Turn back on |
|---|---|---|
| Prompt-time Forum cue (`scripts/forum_cue.py`) | `FORUM_CUE_ENABLED` | `FORUM_CUE_ENABLED=1` |
| Decision-point Forum cue, COR-4911 (`scripts/forum_decision_cue.py`) | `FORUM_DECISION_CUE` | `FORUM_DECISION_CUE=1` |
| `SessionStart` "E3 Collective / Forum" guidance (`scripts/collective_feedback.py`) | `E3_COLLECTIVE_FEEDBACK_HOOK_ENABLED` | `E3_COLLECTIVE_FEEDBACK_HOOK_ENABLED=1` |
| Older `SessionStart` "E3 Collective" text (`scripts/collective.py`, only while the line above is off) | `E3_COLLECTIVE_HOOK_ENABLED` | `E3_COLLECTIVE_HOOK_ENABLED=1` |

`E3_COLLECTIVE_HOOK_ENABLED=0` still overrides the feedback guidance. While the decision-point cue is off it logs no `forum_decision_cue_shown`/`_withheld` events. The sections below describe each cue when it is switched on.

## Sesh open linkage and Forum cue labels at ingest (COR-5027, 0.2.43)

Finished `open_session_passages` events (any `mcp__<namespace>__sesh__open_session_passages`) keep
`sesh_open_search_request_id` (the canonical `search_request_id` argument the open handle carries),
`sesh_open_kind` (`open`, or `feedback` for a "did it help" call, which reads nothing),
`sesh_open_offset` (0-1000, reads only) and, for reads, `sesh_opened_source_handle_sha256_v1` =
`sha256("v1\nsesh__open_session_passages\n" + {"revision_id","session_id"} as sorted compact JSON)`
with witness `failed` or `unknown` (passages are never `verified` exact bytes). Search events add the
same digest for every `open_session_passages` handle in `results[].open`, `results[].source` and
evidence sources; evidence the server marks `available: false` is skipped. The query, passages and
`helped_reason` never leave the machine. The ingest sanitizer keeps these keys always on, like the
older search/open linkage, and only when both copies agree.

The sanitizer also keeps the COR-4911 decision-cue labels on `forum_decision_cue_shown` /
`_withheld` (Codex and Claude Code): `forum_decision_cue` (must match the event type),
`forum_decision_cue_reason` (`first_edit` or `call_N`, N 1-50), `forum_decision_cue_index` (1-10),
`forum_searches_in_turn_before` (0-1000) and `forum_decision_cue_version` (a date). Always on: each
is a closed enum, a small integer or a date. All of these are per-event and never merged onto
`codex_sessions.metadata`.

## Rollout chunk cleanup (COR-4993, 0.2.42)

A rollout chunk file exists only until its record uploads, then it is deleted. Every drain also deletes chunk files that no queue record (pending, processing or dead-letter) references and that are over an hour old; the drain result reports `pruned_chunks` when it removed any.

## Forum decision-point cue (COR-4911, 0.2.41; off by default since 0.2.44)

`scripts/forum_decision_cue.py` (byte-identical in Codex and Claude Code Session Logging) adds one `PreToolUse` note per turn, at the agent's first edit (`apply_patch`, or `Edit`/`Write`/`MultiEdit`/`NotebookEdit`) or its 6th tool call, whichever comes first, in an e3-solutions repository: run ONE `forum__search_research_posts` with 3-8 words naming the general mechanism, not case names. At most 3 per session; `UserPromptSubmit` starts a new turn. 50% of sessions are a holdout by session-id hash; both arms log a content-free exposure event (`forum_decision_cue_shown` / `forum_decision_cue_withheld`). Local state under `forum-cue/decision/` holds counters only. Off with `FORUM_DECISION_CUE=0` (also `false`, `no`, `off`).

## Forum cue v2 A/B (COR-4883, 0.2.39, off by default)

`FORUM_CUE_AB=1` (or `true`, `yes`, `on`) splits sessions that get the Forum decision cue
(`scripts/forum_cue.py`, shared by Codex and Claude Code). **Unset, every session gets the v1 cue
exactly as before and nothing new is logged.** Gating (E3 repositories, first prompt, then a
25+ word new task at most once an hour) is the same in every arm.

- Arms come from the session id alone: `h = sha256("forum_cue_v2:" + lowercased, trimmed id)`;
  `v1` when the low bit of `h[0]` is 0 (50%, today's cue), else `v2_midtask` when the low bit of
  `h[1]` is 1 (25%), else `v2` (25%). In Postgres:
  `case when get_byte(h,0)%2=0 then 'v1' when get_byte(h,1)%2=1 then 'v2_midtask' else 'v2' end`
  with `h = sha256(convert_to('forum_cue_v2:' || lower(btrim(session_id)), 'UTF8'))`.
- `v2`: same search timing as v1, but names the Cosmos tool, rates only posts the agent used or
  checked (one `give_collective_feedback` call with `request.assessments`, up to 5 posts, before
  finishing; unused results left unrated; off-topic ones as abstain starting "Off-topic for this
  query:"; down only with contrary evidence), and allows at most 2 duplicate-check searches
  before `share_with_collective`. `v2_midtask`: v2 plus "do not search yet; search once you have a
  specific cause or mechanism, with its nouns, and again when the plan changes".
- While on, the cue log line gains `experiment` and `arm`, and the `thread_prompt_submitted`
  session event for a cued prompt gains `forum_cue_experiment`, `forum_cue_arm` and
  `forum_cue_reason` metadata. No schema change.

## Fewer routine Sesh searches A/B (COR-4808, on by default since Oct 16, 2026)

An Oct 16, 2026 study of 58 searches where Sesh showed an answer that the agent ignored
found 72% were redundant and 59% were routine (fired at task start, by habit, or because
an instruction said to search). The only routine Sesh cue that E3 ships is the
"Sesh prior-work context" cue above: the Sesh MCP tools, the Cosmos gateway and the
search-coding-sessions skill do not ask for a search up front. This A/B turns that cue
off for half of sessions. **It ships on (Oct 16, 2026); `E3_SESH_ROUTINE_AB=0` turns it off and the
hook output and logged events are then exactly the same as before.**

- On unless `E3_SESH_ROUTINE_AB` is `0`, `false`, `no` or `off`.
- Arms: `routine` (today's cues) or `need_only` (no routine Sesh cue: no startup cue,
  and no compaction cue even with `E3_SESH_CUE_AFTER_COMPACTION=1`). The arm depends
  only on the session id: the low bit of the first byte of
  `sha256("sesh_routine_v1:" + lowercased session id)`, `0` = `routine`,
  `1` = `need_only`. It has no time or week salt, so it never changes for a session.
  In Postgres:
  `get_byte(sha256(convert_to('sesh_routine_v1:' || lower(btrim(session_id)), 'UTF8')), 0) % 2`.
- In `need_only`, Sesh is untouched: the Cosmos tools, the need-based
  search-coding-sessions skill and all search logging (including `sesh_request_id`)
  stay, so the agent still searches when it lacks something.
- These keep today's cue in both arms, as in COR-4681: automated threads (bots keep
  Sesh), `E3_SESH_EXPERIMENT_OPTOUT=1`, and threads whose rollout header or session id
  cannot be read. `E3_SESH_CONTEXT_ENABLED=0` still removes the cue for everyone.
- Nothing new is logged. The readout recomputes the arm from the Codex session id
  that already carries each Sesh search's `sesh_request_id`.
- With the COR-4681 experiment also on, a session sees the cue only when both arms
  allow it, and the COR-4681 log shows only its own arm. Run one at a time.

## Sesh fires on need (COR-4811, both on by default since Oct 16, 2026)

An Oct 16, 2026 study labelled the moment before 107 Codex Sesh searches. Searches right
after a person asked about earlier work helped about 55% of the time; a second search in
the same turn helped 0 of 51 times. Two switches act on that. **Both ship on for everyone
(0.2.38, Oct 16, 2026); setting a variable to `0`, `false`, `no` or `off` turns that one off.**
With both off, hook output and logged events are exactly those of 0.2.36.

- `E3_SESH_ONE_PER_TURN` (on unless `0`, `false`, `no` or `off`), PreToolUse: the first Sesh
  coding-session search in a turn goes ahead; the next one in the same turn (same
  session and `turn_id`) is denied with a short note to use the results it has or open
  one. The next turn resets it. `open_session_passages`, `find_similar_sessions`,
  `ask_sesh` and every other tool are never blocked. Automated threads (bots keep Sesh),
  `E3_SESH_EXPERIMENT_OPTOUT=1`, unreadable threads and calls without a turn id are
  never limited, and any error allows the call. State is one empty marker file per
  session under `$CODEX_HOME/sesh-turn-guard` (hashed session and turn ids, no text). A
  denied call's existing `tool_call_started` event carries `sesh_turn_guard: denied`.
- `E3_SESH_PAST_WORK_CUE` (on unless `0`, `false`, `no` or `off`), UserPromptSubmit: when a person's own
  message (thread source `user`, under 2,500 characters) in an e3-solutions repository
  asks about earlier work, the hook adds one line asking for one Sesh search before
  answering. The pattern is the study's tight one (phrases like "we used to", "which
  change", "what testing did we", "before we switched", "a teammate's sessions"), with
  receipts, relays and pasted instructions excluded. It was about 73% precise on the
  prompts it was built from, against 26% for a plain past-work regex; expect less on new
  prompts. Bots, `E3_SESH_EXPERIMENT_OPTOUT=1`, `E3_SESH_CONTEXT_ENABLED=0` and
  `E3_SESH_START_SEARCH_ENABLED=0` get no cue. It shares one hook output with the Forum
  cue. Nothing new is logged or stored.
- Both work alongside the COR-4808 A/B in either arm: they are need-based, not routine.

## Collective prompt contract

The full contribution must lead with a reusable lesson, method, failure mechanism, or qualified hypothesis—not merely a general title above an operational report. Customer, repository, flag, and incident details belong in a dated `Evidence/example` section with authorized provenance. The lesson should remain useful without that example; query authoritative systems for current state. Search and read relevant prior Forum work before asserting a correction, and link it as supporting, challenging, or superseding evidence. Submissions stay private pending approval; no useful learning is a valid outcome. After fully reading a post, record one explained `up`, `down`, or neutral `abstain` assessment before task completion when feedback is enabled and authorized. Use the exact post id and body hash, preserve the mutation UUID and identical payload for uncertain retries, and report definitive failures as unsaved. Optional specific concerns and USE/SKIP prose do not replace that saved assessment.

Guidance is limited to `SessionStart` sources `startup`, `resume`, and `compact`; missing or unknown sources are silent. Ordinary prompt/tool hooks and `Stop` do not inject it. Local hook tests prove emitted text and event guards, not that an agent reliably follows the prompt. Validate fresh-session and post-compaction task behavior before global distribution.

Captures complete Codex parent and subagent activity through lifecycle hooks, including the exact native rollout JSONL bytes for messages, tool calls, tool outputs, reasoning records, and future record types.

The plugin treats Supabase Storage as the canonical location for full message/event payloads and Supabase Postgres as the queryable catalog. Hook scripts always spool locally first under `~/.codex/session-logging`, then start `scripts/drain_queue.py` in the background to POST queued records to the shared ingest endpoint.

At `SessionStart`, `UserPromptSubmit`, and `Stop`, the plugin reads Codex's native SQLite `threads` table to discover parent and subagent rollout paths, then captures only new bytes from those JSONL files. `PostToolUse` performs the same sweep only after agent-coordination tools such as spawn, wait, follow-up, or interrupt. Each immutable chunk is written to the durable local queue before its checkpoint advances. Complete JSONL lines are also inspected for Codex's cumulative token-count snapshots; every distinct fresh input, cached input, non-reasoning output, reasoning output, total, and context-window observation is queued. Codex's inclusive input/output counters are normalized into those additive components, and internally inconsistent snapshots are ignored. A repeated hook, network failure, concurrent hook, or crash therefore reuses deterministic identities without duplicating remote objects or regressing latest token totals. Even an unterminated crash tail is retained byte-for-byte, later appends continue at its exact offset, and file replacement starts a new generation. Parser state is bounded to a 1 MiB partial line. A per-database activity watermark avoids reopening unchanged historical rollouts on every hook.

There is no per-minute transcript poller and no external session process. A crash tail is recovered by the next lifecycle hook. The resident process remains responsible only for checking plugin updates every 30 minutes. Existing one-minute presence schedulers are removed automatically during upgrade.

Capture is scoped to repositories whose `origin` remote belongs to the `e3-solutions` GitHub organization. In other repositories, the hooks return without writing local or remote session data.
### Opt-in E3 directories outside a repo (COR-4900, off by default)

Some E3 work runs from a scratch directory that is not a git repository, so it has no `origin` and is skipped. To count such a directory as E3, list it, either as `CODEX_SESSION_LOG_EXTRA_DIRS` (absolute paths separated by `:`) or one absolute path per line in `~/.codex/session-logging/extra-dirs` (or `$CODEX_SESSION_LOG_STATE_DIR/extra-dirs`). For a session whose cwd is that directory or below it **and has no git `origin`**, the plugin captures as for an E3 repository, adds the SessionStart Collective guidance, and shows the Forum decision cue. A real `origin` always wins, so a non-E3 repository inside a listed directory stays out. Relative paths, `/` and the home directory itself are ignored.

These sessions upload the fixed `repo_remote` `https://github.com/e3-solutions/allowlisted-dir.git` (no repository has that name; it is shaped like an in-org remote so the existing ingest accepts it). No new path is uploaded; `cwd` is sent exactly as for repository sessions. The client refuses to upload that marker unless an extra directory is configured. Nothing changes for anyone who does not set this.


Off by default since COR-5034 (see "Forum cues off by default"). When switched on, for eligible E3 sessions, `SessionStart` adds a compact reminder about the E3 Collective at startup, resume, and post-compaction context rebuild. It tells agents to use authoritative systems for directly queryable current state, consult published Forum work for relevant prior reasoning, and submit only reusable non-queryable insights or material corrections linked as supporting, challenging, or superseding prior work. `Stop` remains logging-only and never emits Collective guidance, so ordinary completions cannot create an extra agent turn. The hook itself never contacts Forum or submits content; the agent decides whether there is anything useful and finishes normally when Forum is unavailable. Guidance excludes secrets, private or restricted material, routine status, raw logs, and duplicates, and asks for observation time and source provenance when needed. Set `E3_COLLECTIVE_HOOK_ENABLED=0` to disable the reminder.

Queryable hook event rows record only the tool name, phase, optional tool call id, and success flag when exposed by Codex. The private rollout objects retain the exact native JSONL, including tool arguments and outputs, so no available session data is discarded. Setup snapshots include sanitized Codex config names such as enabled plugins, installed skill names/sources, MCP server names/transport, marketplaces, app connection ids/tool names, and non-secret model/runtime settings.

Each runtime `session_id` is retained for event correlation. When Codex provides a `transcript_path`, the plugin also records a SHA-256 `thread_id` derived from that path so resumed runtime sessions can be grouped as one conversation without storing another copy of the path. Legacy records without a transcript reference remain separate runtime sessions and cannot be grouped reliably after the fact.

## Legacy historical backfill

Historical transcript backfills are disabled as of version 0.2.2. `SessionStart` captures only the live environment snapshot and no longer launches `backfill_sessions.py`. The ingest Edge Function acknowledges and discards historical-backfill data and status payloads so queues created by older plugin versions can drain without modifying Storage or Postgres.

The legacy importer and its checkpoint files remain in the repository only for auditability. Do not run it; historical analysis uses the separately imported `ai_session_*` archive tables.

The 0.2.8 rollout synchronizer does not parse or reconstruct legacy transcript messages. It only extracts cumulative usage while capturing native rollouts already admitted by the live 24-hour discovery policy. Its first hook considers only native E3 tasks active during the previous 24 hours, prioritizes the current task family, and captures at most 8 MiB across 32 rollout files before returning. Durable pending rows and byte offsets continue that bounded baseline on later hooks, including when SQLite exposes a row before its rollout file exists. Older history remains under the legacy archive policy instead of being auto-uploaded during upgrade. After installation, the SQLite watermark discovers new or changed parent and subagent tasks without rescanning lifetime history.

## Supabase

Project: `codex-session-logging`

Project ref: `pmdfllwuctzkdjiehezq`

Default URL:

```bash
https://pmdfllwuctzkdjiehezq.supabase.co
```

Apply the SQL files in `supabase/migrations` when provisioning a new project. Chunk objects use the existing private bucket and their offsets, hashes, generations, and parent/root relationships are cataloged in `codex_session_events`. Version 0.2.11 adds immutable usage observations to the existing nine-argument `upsert_codex_session_usage_latest` RPC. It derives the immutable owner from `codex_sessions`, records every distinct observation, and advances the latest row only when its timestamp and all four cumulative token components do not move backwards. Deploy that migration before updating clients. The Edge Function, RPC, and database constraint require the normalized token components to sum exactly to the reported total.

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

To recover usage from rollout chunks stored before 0.2.11, run the bounded admin replay in dry-run mode first. It reads the existing event catalog with a fixed cutoff and keyset cursor, defaults to the previous 72 hours, 500 sessions, and one session reader, verifies chunk offsets, sizes, and hashes, and makes no database writes unless `--apply` is passed. Use `--workers 4` for bounded session-level parallel reads. A resume must reuse the reported `cutoff` with `--cutoff` and the reported `resume_after_session` with `--after-session`. A later fresh run safely picks up concurrent catalog inserts; the monotonic RPC makes reruns idempotent.

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

Usage requests authenticate with the session's installation capability. `CODEX_SESSION_LOG_INGEST_TOKEN` is an optional additive gate for all requests; configure it only when every client has also been provisioned with that token.

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
