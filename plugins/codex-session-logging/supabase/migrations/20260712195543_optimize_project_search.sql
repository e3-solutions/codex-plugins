drop index if exists public.codex_sessions_repo_user_idx;
drop index if exists public.ai_session_transcripts_repo_user_idx;

create index codex_sessions_project_user_idx
  on public.codex_sessions (
    (
      nullif(
        lower(
          regexp_replace(
            regexp_replace(
              trim(repo),
              '^(https?://github\.com/|ssh://git@github\.com/|git@github\.com:)',
              '',
              'i'
            ),
            '(\.git)?/?$',
            '',
            'i'
          )
        ),
        ''
      )
    ),
    user_id
  )
  where repo is not null;

create index ai_session_transcripts_project_user_idx
  on public.ai_session_transcripts (
    (
      nullif(
        lower(
          regexp_replace(
            regexp_replace(
              trim(repo_remote),
              '^(https?://github\.com/|ssh://git@github\.com/|git@github\.com:)',
              '',
              'i'
            ),
            '(\.git)?/?$',
            '',
            'i'
          )
        ),
        ''
      )
    ),
    user_id
  )
  where repo_remote is not null;
