---
name: ingest-change
description: Change the codex-session-ingest Supabase Edge Function or the session tables it writes (codex_sessions, codex_session_messages, codex_session_events, codex_session_users), apply the schema through migration history, and deploy the function by name in the right order. Use for ingest bugs, new captured fields, label or linkage changes, and ingest deploys or rollbacks.
---

# Ingest change

Background (tables, chunks, ignore-session protocol, usage replay): the
"Supabase" section of `plugins/codex-session-logging/README.md`.
Function code: `plugins/codex-session-logging/supabase/functions/codex-session-ingest/`.
Project: `pmdfllwuctzkdjiehezq`.

## Rules

- **Order: schema, then function, then clients.** The function must accept what
  old clients still send, and old function versions must survive the new schema.
  Ship client changes (`release-plugin` skill) last.
- **Schema only through migration history.** This Supabase project also holds
  the Sesh and Jolly Roger schemas, so its history has versions this folder does
  not. `supabase db push` from here sees them as unknown. Apply the new file
  through migration history (Supabase MCP `apply_migration`, using the file's name
  and SQL), then check it is recorded. Never paste DDL into the SQL editor.
- **Deploy only this function, by name**, from a clean worktree at `origin/main`.
  A bare `supabase functions deploy` deploys every function in the folder. On
  Nov 4 that replaced a Sherlock function with an unrecorded version.
- `supabase secrets set` restarts every function in the project. Only change
  secrets with a yes, and outside busy hours.
- Never print `SUPABASE_SERVICE_ROLE_KEY` / `SUPABASE_SECRET_KEYS`.

## Steps

1. Write the migration as a new file in
   `plugins/codex-session-logging/supabase/migrations/` (newer timestamp,
   backward compatible). For RPC changes, run the opt-in DB checks against a
   disposable database, for example
   `python3 plugins/codex-session-logging/supabase/scripts/test_usage_rpc_db.py`.
2. Change the function and its tests. From the function folder:

   ```bash
   deno test --allow-env --allow-net=127.0.0.1,localhost --no-lock .
   ```

   CI runs the same command (`.github/workflows/ingest-deno-tests.yml`).
3. After merge, with approval: apply the migration, then deploy:

   ```bash
   supabase functions deploy codex-session-ingest \
     --workdir plugins/codex-session-logging \
     --project-ref pmdfllwuctzkdjiehezq --no-verify-jwt --use-api
   ```

   Without `--workdir`, the CLI fails with "Entrypoint path does not exist".
4. Verify:
   - Edge function logs show 2xx for new sessions, and no new 4xx or 5xx spike.
   - A new session row and its events appear for your own test session.
5. Rollback: redeploy the previous commit's function the same way, by name. A
   migration is never rolled back by editing history; add a new one.
6. Add a Linear comment for each production step: migration applied, function
   version deployed, rollback target.

## Report

Migration name and whether it was applied, the function version before and after,
test results, log check, and the client release if any.
