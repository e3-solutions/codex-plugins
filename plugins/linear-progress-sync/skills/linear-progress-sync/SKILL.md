---
name: linear-progress-sync
description: Use when working with the local Linear progress sync plugin or when Linear kickoff, active state, PR linking, or progress sync behavior affects a Codex task.
---

# Linear Progress Sync

Small contract only; `/linear-start` owns the runbook.

- Scope is `e3-solutions` repos.
- Before Codex file edits, `.codex/linear-sync/active.json` must exist.
- Guard only blocks Codex file edit tools such as `apply_patch`; general Bash is allowed.
- If active state or repo binding is missing, run `/linear-start`; do not patch, ask for an issue key, test writes, or stop after issue creation.
- `/linear-start` handles profile, binding, What / Why / How issue creation, PR link/comment, activation, and opt-out.
- Never move Linear issues to terminal states. Use Codex Linear tools, not a direct API client.
