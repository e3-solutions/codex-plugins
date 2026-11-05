# codex-plugins agent guide

This file is loaded into every Claude Code and Codex session, so it stays short.
[docs/README.md](docs/README.md) routes each kind of task to the deeper doc it needs.

## What this repo is

The Core Edge plugin marketplace for Codex (`.agents/plugins/marketplace.json`)
and Claude Code (`.claude-plugin/marketplace.json`, marketplace `coreedge-internal`).

| Plugin | Tool | What it does |
| --- | --- | --- |
| `plugins/linear-progress-sync` | Codex | Linear progress sync, and the **resident updater** that rolls out every Codex plugin |
| `plugins/codex-session-logging` | Codex (+ shared scripts) | Session capture to Supabase `pmdfllwuctzkdjiehezq`. Ingest Edge Function in `supabase/functions/codex-session-ingest` |
| `plugins/claude-session-logging` | Claude Code | Claude session capture. Updates itself daily from the marketplace |
| `plugins/sesh`, `plugins/claude-sesh` | Codex, Claude Code | Sesh search skill and cues |

## Agent install contract

When a user asks you to install these plugins, do **not** run `codex plugin add`
with the GitHub URL or the repo root. That skips setup. Use:

```bash
git clone https://github.com/e3-solutions/codex-plugins
cd codex-plugins
gh auth login
python3 plugins/linear-progress-sync/scripts/setup.py
codex mcp login linear
codex mcp login e3-cosmos
```

Then tell the user to restart Codex. If Codex asks to review hooks, they should
trust the Linear Progress Sync and Codex Session Logging hooks once. New machines
usually get all of this through Jolly Roger instead (`e3-solutions/jolly-roger`).

## How changes reach machines

- **Codex plugins:** the resident updater reads
  `plugins/linear-progress-sync/update-manifest.json` on `main`. Machines update
  only when that version changes (`release-plugin` skill).
- **Jolly Roger** pins this repo to one commit in its `repositories.json`. Bumping
  that pin is a separate PR in jolly-roger.
- **Claude plugins** update from the marketplace once a day when their version changes.
- A merge that bumps no version ships nothing. **A merge that bumps the manifest
  ships to every engineer's machine**, so it needs an explicit yes.

## Rules for every task

- Register hooks only in each plugin's own `hooks/hooks.json`, never in
  `~/.codex/hooks.json`. Duplicate registration writes duplicate log rows.
- Ingest changes: apply the migration before deploying the function, and deploy
  the function by name only (`ingest-change` skill).
- Tests: `python3 -m pytest -q tests` for the Python plugins. In
  `plugins/codex-session-logging/supabase/functions/codex-session-ingest`, run
  `deno test --allow-env --allow-net=127.0.0.1,localhost --no-lock .`
  (`.github/workflows/ingest-deno-tests.yml`).
- Never commit or print keys. `SUPABASE_SERVICE_ROLE_KEY` and `SUPABASE_SECRET_KEYS`
  live only in the function's secrets, never on developer machines.
- Linear: move the issue as the work moves. Branch opened → `In Progress`. PR
  opened → `In Review`. Merged and accepted → `Done`. Pushes to `main` also
  enqueue commits for Linear sync (`.github/workflows/linear-sync.yml`).

## Skills

Repeated tasks have skills in `.agents/skills/` (Codex reads them there;
`.claude/skills/` links to the same files for Claude Code). The skills inside
`plugins/*/skills/` are different: they ship to users.

- `release-plugin`: bump versions and roll a plugin change out to machines.
- `ingest-change`: change the session-ingest Edge Function or its schema, and deploy it safely.
- `hook-change`: add or change a Codex or Claude hook without duplicating or breaking capture.
