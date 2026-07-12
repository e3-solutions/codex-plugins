# Export e3 Codex and Claude sessions

Use the standalone exporter when someone needs immediate access to original local transcripts and does not need them indexed in Supabase.

```bash
python3 scripts/export_e3_sessions.py
```

The command scans `~/.codex/sessions` and `~/.claude/projects`, includes only sessions that can be verified as belonging to a GitHub `e3-solutions` repository, and writes a ZIP to the Desktop. The archive contains original JSONL transcript structure, a manifest, repository-verification details, checksums, and counts.

Common credential patterns are redacted by default. Review the archive before sharing it because free-form messages and tool output can still contain sensitive business or customer data that no automatic filter can identify.

Useful options:

```bash
# Choose the destination.
python3 scripts/export_e3_sessions.py --output ~/Desktop/priyal-e3-sessions.zip

# Preview counts and selected paths without creating an archive.
python3 scripts/export_e3_sessions.py --dry-run

# Preserve exact source bytes for a highly trusted recipient.
python3 scripts/export_e3_sessions.py --raw --output ~/Desktop/e3-sessions-raw.zip
```

The exporter never contacts Supabase or any other network service. Sessions that cannot be verified as belonging to `e3-solutions` are omitted instead of risking unrelated personal data.

## Import archives for Supabase MCP queries

The archive importer writes every JSONL line into queryable Postgres rows. It requires an authenticated Supabase CLI profile that can retrieve the project's service-role key; the key is held in memory and never printed.

```bash
python3 scripts/import_session_archives.py \
  --project-ref pmdfllwuctzkdjiehezq \
  --archive 'USER_UUID=/absolute/path/to/export.zip'
```

Repeat `--archive` to import multiple owners in one run. Imports use deterministic UUIDs and Postgres upserts, so retrying the same archive is safe.

The MCP-queryable tables are:

- `ai_session_imports`: archive status, owner, manifest, and aggregate counts.
- `ai_session_transcripts`: one row per Codex or Claude JSONL transcript.
- `ai_session_records`: one row per source line with the complete JSON object in `payload`, plus searchable platform, type, subtype, timestamp, role, tool name, and excerpt fields.

Malformed JSON, PostgreSQL-incompatible `U+0000` values, and individual records larger than 500 KB are preserved exactly in `raw_text` with `parse_error` instead of being dropped. Oversized records still retain locally extracted type, subtype, timestamp, role, tool, and excerpt fields. Example MCP SQL:

```sql
select platform, record_type, record_subtype, count(*)
from public.ai_session_records
where user_id = 'USER_UUID'
group by platform, record_type, record_subtype
order by count(*) desc;

select occurred_at, role, tool_name, content_excerpt, payload
from public.ai_session_records
where transcript_id = 'TRANSCRIPT_UUID'
order by seq;
```

## Unified live and archive analysis

Use the canonical read-only views instead of manually unioning live tables with archive imports:

- `session_analysis_sessions`
- `session_analysis_messages`
- `session_analysis_tool_events`
- `session_analysis_usage`
- `session_analysis_models`

Every view exposes `source` (`live` or `archive`) and `platform` (`codex` or `claude`). Message and tool views also expose `has_live_session`; use `is_canonical` on messages to exclude duplicate Codex message representations.

`session_analysis_usage` is already deduplicated: it prefers the typed live Codex usage row, otherwise selects the final cumulative Codex token snapshot, and sums per-response Claude usage for each transcript.

```sql
select platform, model, count(*) as sessions
from public.session_analysis_models
group by platform, model
order by sessions desc;

select platform, sum(total_tokens) as total_tokens
from public.session_analysis_usage
group by platform;
```
