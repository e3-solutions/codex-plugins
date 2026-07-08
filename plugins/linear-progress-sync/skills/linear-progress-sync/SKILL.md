---
name: linear-progress-sync
description: Use when working with the local Linear progress sync plugin or when Linear kickoff, active state, PR linking, or progress sync behavior affects a Codex task.
---

# Linear Progress Sync

Keep this skill small: it is loaded for plugin work and should not duplicate the `/linear-start` runbook.

## Guard Contract

- Enforcement applies only to repos whose `origin` is under `e3-solutions`.
- Before the first Codex file edit, check `.codex/linear-sync/active.json`.
- The pre-tool guard blocks documented Codex file edits through `apply_patch` until active Linear state exists.
- General Bash commands are allowed before kickoff; do not use shell command classification as the Linear guard boundary.
- Repos can opt out with `linear_start.py configure-repo --disable-linear-sync --reason "<reason>"`.

## When Active State Is Missing

Run `/linear-start` or `scripts/linear_start.py` instead of hand-rolling the flow. The command docs own the full sequence: user profile, repo binding, issue creation or lookup, draft PR, Linear link/comment confirmation, and `active.json` activation.

If a guard message says Linear destination or kickoff is required, continue that workflow yourself. Do not answer with a patch, ask for a Linear issue key, test write access, or stop after creating only the issue.

## Safety Rules

- Never move Linear issues to terminal states.
- Only comment or move a confirmed non-terminal issue to `In Progress`.
- Use the existing Codex Linear MCP/app tools; do not add a direct Linear API client.
- End Linear issue bodies and comments Codex creates with `Codex bot: <stored Linear user name> at <ISO-8601 UTC timestamp>`.
- Sync progress from the active Linear issue first; branch/commit inference is fallback only.
