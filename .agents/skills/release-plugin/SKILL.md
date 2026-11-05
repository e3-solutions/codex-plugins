---
name: release-plugin
description: Bump plugin versions in codex-plugins and roll a change out to engineers' machines through the resident updater, the Claude marketplace and the Jolly Roger pin, then confirm machines picked it up. Use when shipping any plugin change (logger, Sesh, Linear sync, Claude logging), adding a default plugin, or asking why machines still run an old version.
---

# Release a plugin

The rollout steps are in the README section "Rolling Out Updates". This skill
adds the files that move together and the checks that recent releases used.

## Rules

- **Any merge that changes files in a default plugin (`plugins/linear-progress-sync/`,
  `plugins/codex-session-logging/`) ships to every resident machine within about
  30 minutes, with or without a version bump.** The resident updater replaces any
  plugin whose file digest changed. Get an explicit yes before merging, and still
  bump versions so presence reports can tell releases apart.
- One release, one PR. The PR title lists every new version, for example
  "(logger 0.2.44, resident 0.3.44, Claude 0.2.22)".
- New behaviour that could add noise or cost ships **off by default** behind an
  env flag. Document the flag in the plugin README.

## Files that move together

| Changed | Bump |
| --- | --- |
| `plugins/codex-session-logging/` | `.codex-plugin/plugin.json` `version`, `.jollyroger.json` `version`, `PLUGIN_VERSION` in `scripts/session_logging.py` |
| `plugins/sesh/` (not a default plugin) | `.codex-plugin/plugin.json`, `.jollyroger.json`, `PLUGIN_VERSION` in `scripts/session_logging.py`. After the merge, `COMMIT` and `VERSION` in `plugins/sesh/install.py` |
| `plugins/claude-session-logging/` | `.claude-plugin/plugin.json` |
| `plugins/claude-sesh/` | `.claude-plugin/plugin.json`, `PLUGIN_VERSION` in `scripts/sesh_cc.py`, and the `sesh` `version` in `.claude-plugin/marketplace.json` (`tests/test_claude_sesh.py::test_versions_agree`) |
| A default Codex plugin | `plugins/linear-progress-sync/.codex-plugin/plugin.json` **and** `plugins/linear-progress-sync/update-manifest.json`, kept **equal**. A mismatch makes every machine's update fail ("does not match archive plugin") |
| A new default Codex plugin | Its entry in `.agents/plugins/marketplace.json` with `policy.installation: INSTALLED_BY_DEFAULT` |

- `plugins/sesh/scripts/session_logging.py` and
  `plugins/claude-session-logging/scripts/session_logging.py` are separate files
  from the Codex logger's, but releases often change all three. Check whether
  your fix belongs in each.
- Tests pin versions: search `tests/` for the old version string and update the
  pins (for example `tests/test_codex_session_presence.py`, `tests/test_linear_progress_sync.py`).
- Add a short README section to each changed plugin: `## <what> (COR-xxxx, <version>)`.

## Steps

1. Make the change. Run `python3 -m pytest -q tests`, plus the Deno tests if the ingest function changed.
2. Bump the files above, then grep for the old versions to catch stragglers.
3. Open the PR. Merge only with the yes.
4. **Jolly Roger machines** follow the codex-plugins commit pinned in
   jolly-roger `repositories.json`. Bumping that pin is a separate jolly-roger PR
   (capture stream). Coordinate it.
5. **Confirm uptake.** Presence reports carry the plugin version (`plugin.version`).
   On one machine, `python3 plugins/linear-progress-sync/scripts/update_plugin.py --doctor`
   checks that the resident updater is installed and active. The ingest function
   does not store the plugin version on session rows.

## Report

Versions bumped, PR, merge time, Jolly Roger pin PR if any, and how many machines
report the new version (with the query or tool used).
