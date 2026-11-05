---
name: hook-change
description: Add or change a Codex or Claude Code hook in codex-plugins (SessionStart, UserPromptSubmit, PreToolUse, PostToolUse, Stop and others) without double-registering, blocking the agent, or breaking session capture and the resident updater. Use for new cues, capture fields, hook errors, slow hooks, or duplicate log rows.
---

# Hook change

## How hooks are wired

- **Codex:** each plugin registers its hooks only in its own `hooks/hooks.json`.
  Codex loads plugin hooks natively. Each command finds the newest cached copy of
  the script, and exits 0 if none is found:
  `ls -td "$HOME"/.codex/plugins/cache/*/<plugin>/*/scripts/<hook>.py | head -n 1`.
  `setup.py` removes old Core Edge copies from `~/.codex/hooks.json`, so never add
  hooks there. A hook registered in both places runs twice and writes duplicate
  log rows.
- **Claude Code:** `plugins/claude-session-logging/hooks/hooks.json` and
  `plugins/claude-sesh/hooks/hooks.json` call scripts through `${CLAUDE_PLUGIN_ROOT}`.
- **Updater:** `plugins/linear-progress-sync/scripts/session_start.py` and
  `pre_tool_use.py` call `ensure_resident_updater` (and session start calls
  `maybe_spawn_auto_update`) near the top. Keep those calls first, so a hook
  failure lower down cannot stop updates.

## Rules

- **Fail open.** A hook must never block or crash the agent's turn. Catch
  errors, log a bounded reason, and exit 0. Capture failures go to the durable
  local queue, not to the user.
- **Fast.** Hooks run on every event. Do no network calls on the hot path; use
  the queue and drain.
- **Cues and new behaviour ship off by default** behind an env flag, with an
  experiment or holdout if they change agent behaviour. Forum cues are off since
  0.2.44, while Forum is hidden.
- **Capture only metadata the contract allows.** Never add raw secrets. Respect
  owner exclusions and the `--ignore-extension` protocol (plugin README).

## Steps

1. Edit the script under `plugins/<plugin>/scripts/`. Change `hooks/hooks.json`
   only to add or remove an event.
2. Add tests in `tests/`, for example `tests/test_codex_session_logging.py`,
   `tests/test_claude_session_logging.py` or `tests/test_collective_hooks.py`.
   Cover: the event is captured once, a failure is non-blocking, and the flag-off
   path is unchanged. Tests share a debounce cache, so a quick rerun can fail
   for that reason alone. Isolate state per test.
3. Run `python3 -m pytest -q tests`.
4. Release it (`release-plugin` skill). Hooks that need new trust on a machine
   (a new event or command) prompt users to review hooks again. Say so in the PR.

## Report

Events and plugins touched, flag name and default, tests, and whether users will
see a hook-trust prompt.
