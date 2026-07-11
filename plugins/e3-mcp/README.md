# E3 MCP

Codex plugin for the E3 MCP gateway at `https://e3-mcp-production.up.railway.app/mcp`.

The gateway exposes E3/Core Edge organization data through namespaced MCP tools, including meetings, chat, calendars, Linear, GitHub, Salesforce, email, and coding-agent usage. Available sources and tools depend on the access tier attached to your code.

## Authentication

The plugin reads its bearer access code from `E3_MCP_ACCESS_CODE`. Never commit an access code or put one in project configuration.

Obtain an `e3_...` access code from an E3 MCP administrator or the gateway console, then expose it to the process that launches Codex:

```bash
export E3_MCP_ACCESS_CODE='e3_...'
```

For the macOS desktop app, set the variable in the launch environment and fully restart Codex:

```bash
launchctl setenv E3_MCP_ACCESS_CODE "$E3_MCP_ACCESS_CODE"
```

Avoid pasting the real value into documentation, chat, source files, or committed shell scripts.

## First use

Start a new Codex task after installing the plugin. Ask Codex to call the gateway's `documentation` tool first; it reports the sources and tools available to your access tier.
