# Agent Install Contract

This repository is a Codex plugin marketplace, not a single plugin source.

When a user asks you to install Core Edge Codex plugins from this repository, do not run `codex plugin add` with the GitHub URL or with the repository root as a direct plugin source. That skips the setup steps that install all default plugins, merge global hooks, and register Linear MCP.

Use this exact install flow:

```bash
git clone https://github.com/e3-solutions/codex-plugins
cd codex-plugins
gh auth login
python3 plugins/linear-progress-sync/scripts/setup.py
codex mcp login linear
```

After setup, tell the user to set `E3_MCP_ACCESS_CODE` in the environment that launches Codex, then restart Codex or start a new Codex thread. Never ask them to commit or paste the access code into repository files. If Codex asks to review hooks, they should trust the Linear Progress Sync and Codex Session Logging hooks once.
