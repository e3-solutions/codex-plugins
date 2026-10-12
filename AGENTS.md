# Agent Install Contract

This repository is a Codex plugin marketplace, not a single plugin source.

When a user asks you to install Core Edge Codex plugins from this repository, do not run `codex plugin add` with the GitHub URL or with the repository root as a direct plugin source. That skips the setup steps that install Codex Session Logging, merge global hooks, and register Linear MCP and E3 Cosmos.

Use this install flow for the remaining default Codex plugin:

```bash
git clone https://github.com/e3-solutions/codex-plugins
cd codex-plugins
gh auth login
python3 plugins/linear-progress-sync/scripts/setup.py
codex mcp login linear
codex mcp login e3-cosmos
```

After setup, tell the user to restart Codex or start a new Codex thread. If Codex asks to review hooks, they should trust the Codex Session Logging hooks once. Setup and resident updates no longer install Linear Progress Sync.

