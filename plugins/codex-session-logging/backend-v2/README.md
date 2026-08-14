# Codex agent analytics v2

This directory is an isolated Supabase backend for complete Codex session
logging. It does not replace or mutate the original `supabase/` backend.

There is intentionally no historical import or compatibility layer. The new
project starts with v2 data only. The existing plugin remains pointed at the
original endpoint until a separate client cutover provisions the v2 ingest
token and envelope; this prevents a silent partial rollout.

## Data contract

- `agent_installations`: stable client and team identity.
- `agent_runs`: root sessions and subagent hierarchy.
- `agent_records`: every message, prompt, tool record, event, reasoning item,
  and usage snapshot. `payload` retains the complete parsed record.
- `private.session_registry`: retry/tombstone state only.
- `agent-rollouts` Storage bucket: immutable, content-addressed source bytes.
- `activity_export_v1`: flattened, versioned egress surface.
- `latest_usage_v1` and `usage_deltas_v1`: derived usage views, not duplicate
  source-of-truth tables.

The ingest function uploads the raw object first and then calls one transactional,
idempotent database function. A retry reuses the content-addressed object and
commits any missing projection rows. A reused record key with different payload
is rejected instead of silently overwriting history.

## Deployment

The linked remote project is `qknnerihgckgufokquph`
(`codex-agent-analytics-v2`, `us-east-1`). Keep secrets out of this repository.
Its initial workspace is `dc5de331-2f6f-422f-9f49-07bf99579ae3`.

The project contains one synthetic acceptance fixture tagged with
`metadata.acceptance_test = true`: one root run, one subagent, complete message
and tool examples, and cumulative usage snapshots. No rows or objects were
copied from the original project.

```bash
supabase db push --workdir plugins/codex-session-logging/backend-v2
supabase functions deploy agent-ingest-v1 \
  --workdir plugins/codex-session-logging/backend-v2
```

The deployed function requires these secrets:

- `CODEX_AGENT_INGEST_TOKEN`
- `CODEX_AGENT_WORKSPACE_ID`

`SUPABASE_URL` and `SUPABASE_SERVICE_ROLE_KEY` are supplied by the Edge Runtime.
The acceptance token was generated without printing or committing it. Rotate
and provision `CODEX_AGENT_INGEST_TOKEN` as part of the client cutover.

## Egress

Query `activity_export_v1` for BI/CSV/Parquet exports. It is one row per record
and already includes actor, installation, run hierarchy, repository context,
message/tool content, usage, the full parsed payload, and the immutable raw
object locator. Use `(occurred_at, record_id)` as the incremental export cursor.

Common questions stay small SQL queries:

```sql
-- Exact user prompt history.
select actor_email, run_id, occurred_at, content_text, payload
from activity_export_v1
where record_kind = 'message' and role = 'user'
order by occurred_at, record_id;

-- Root/subagent tree and activity mix.
select root_run_id, parent_run_id, run_id, agent_role, record_kind, count(*)
from activity_export_v1
group by root_run_id, parent_run_id, run_id, agent_role, record_kind;

-- Latest cumulative usage for a complete root + subagent tree.
select root_run_id, sum(total_tokens) as total_tokens, sum(cost_usd) as cost_usd
from latest_usage_v1
group by root_run_id;
```
