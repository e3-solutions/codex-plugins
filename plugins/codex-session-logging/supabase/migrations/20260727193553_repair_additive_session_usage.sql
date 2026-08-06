-- Normalize only rows whose legacy encoding can be proven from their source
-- and arithmetic. Unknown malformed rows are intentionally left untouched.

-- The disabled Codex historical importer stored cached input and reasoning as
-- both inclusive provider totals and separate components. Subtract only when
-- the provider invariant proves that exact legacy shape.
update public.codex_session_usage
set
  input_tokens = input_tokens - cached_input_tokens,
  output_tokens = output_tokens - reasoning_output_tokens,
  updated_at = now()
where metadata ->> 'source' = 'historical_transcript'
  and input_tokens >= cached_input_tokens
  and output_tokens >= reasoning_output_tokens
  and total_tokens::numeric = input_tokens::numeric + output_tokens::numeric
  and total_tokens::numeric <>
    input_tokens::numeric + cached_input_tokens::numeric
      + output_tokens::numeric + reasoning_output_tokens::numeric;

-- Claude transcript sync preserved cache creation in metadata but included it
-- in total_tokens despite having no matching table component. Recompute the
-- total only when the metadata value proves that exact legacy discrepancy.
with transcript_candidates as (
  select
    session_id,
    input_tokens::numeric + cached_input_tokens::numeric
      + output_tokens::numeric + reasoning_output_tokens::numeric
      as component_total,
    case
      when metadata ->> 'cache_creation_input_tokens' ~ '^[0-9]{1,19}$'
      then case
        when (metadata ->> 'cache_creation_input_tokens')::numeric
          <= 9223372036854775807::numeric
        then (metadata ->> 'cache_creation_input_tokens')::numeric
        else null
      end
      else null
    end as cache_creation_tokens
  from public.codex_session_usage
  where metadata ->> 'source' = 'transcript_sync'
    and metadata ->> 'agent' = 'claude'
)
update public.codex_session_usage as usage
set
  total_tokens = candidates.component_total::bigint,
  updated_at = now()
from transcript_candidates as candidates
where usage.session_id = candidates.session_id
  and candidates.cache_creation_tokens is not null
  and candidates.component_total <= 9223372036854775807::numeric
  and usage.total_tokens::numeric =
    candidates.component_total + candidates.cache_creation_tokens
  and usage.total_tokens::numeric <> candidates.component_total;
