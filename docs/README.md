# Docs router

Start here after `AGENTS.md`. Find your task and read only what it lists.

| Task | Read first | Add only when needed |
| --- | --- | --- |
| Install the plugins for someone | `AGENTS.md` "Agent install contract", the root `README.md` "Setup" | `plugins/linear-progress-sync/README.md` |
| Ship a plugin change to machines | `release-plugin` skill, the root `README.md` "Rolling Out Updates" | the changed plugin's `README.md` |
| Session capture (Codex logger) behaviour, flags, ingest | `plugins/codex-session-logging/README.md` | `ingest-change` skill for the Edge Function and schema |
| Claude Code capture | `plugins/claude-session-logging/README.md` | |
| Sesh cues and search skill | `plugins/sesh/README.md`, `plugins/claude-sesh/README.md` | [SESH_CONTEXT_HOOKS.md](SESH_CONTEXT_HOOKS.md) |
| Add or change a hook | `hook-change` skill | [COLLECTIVE_FEEDBACK_HOOK_CONTRACT.md](COLLECTIVE_FEEDBACK_HOOK_CONTRACT.md), [COLLECTIVE_FEEDBACK_HOOKS.md](COLLECTIVE_FEEDBACK_HOOKS.md) |
| Linear progress sync behaviour | `plugins/linear-progress-sync/README.md` | |

## History, not current guidance

`superpowers/plans/` holds dated implementation plans. Each plugin README's
`## <change> (COR-xxxx, <version>)` sections are its changelog; the newest is at the top.

## Keeping this useful

- `python3 .agents/check_agent_docs.py` checks that every path, module, workflow and
  skill named in these docs still exists. CI runs it on every PR (warn-only until
  2026-11-12, then required). Put correct names it cannot find (other repos, runtime
  files, Supabase refs) in `.agents/doc-check-allow.txt`.
- After a PR merges with code changes but no docs changes, `.github/workflows/docs-backstop.yml`
  asks Claude for a small docs follow-up PR (COR-5202). It skips until the
  `ANTHROPIC_API_KEY` org secret is shared with this repo.
- One fact, one home. Link to it instead of copying it into a skill or `AGENTS.md`.
- Add a row here in the same PR that adds a durable doc.
