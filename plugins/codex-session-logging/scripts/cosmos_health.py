#!/usr/bin/env python3
"""Report once when the E3 Cosmos MCP server is missing or broken (COR-4683).

Without Cosmos, agents silently stop searching Sesh. At SessionStart this reads
Codex config.toml and, only when the status changes, queues one content-free
event: cosmos_mcp_missing, cosmos_mcp_disabled, cosmos_mcp_custom,
cosmos_mcp_unreadable, or cosmos_mcp_restored when it is healthy again. The
status is carried in the event type so the deployed ingest keeps it. Nothing
from the config (URLs, headers, tokens) is sent or logged.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from session_logging import (
    JsonDict,
    capture_metadata_event,
    codex_config_path,
    should_capture_payload,
    state_dir,
    write_json_atomic,
)

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python < 3.11 fallback.
    tomllib = None  # type: ignore[assignment]

COSMOS_MCP_NAME = "e3-cosmos"
# Same hosts the resident updater recognizes (linear-progress-sync resident_updater.py).
COSMOS_GATEWAY_HOSTS = frozenset({"cosmos.e3g.ai", "e3-mcp-production.up.railway.app"})
STATE_FILE = "cosmos-mcp-health.json"
PROBLEM_STATUSES = ("missing", "disabled", "custom", "unreadable")


def is_cosmos_url(value: Any) -> bool:
    if not isinstance(value, str) or not value.strip():
        return False
    try:
        return urlsplit(value.strip().lower()).hostname in COSMOS_GATEWAY_HOSTS
    except ValueError:
        return False


def _scan_servers(raw: str) -> dict[str, JsonDict] | None:
    servers: dict[str, JsonDict] = {}
    current: str | None = None
    for line in raw.splitlines():
        header = re.match(r"^\s*\[\s*mcp_servers\s*\.\s*(?:\"([^\"]+)\"|'([^']+)'|([A-Za-z0-9_-]+))\s*\]\s*(?:#.*)?$", line)
        if header:
            current = next(group for group in header.groups() if group is not None)
            servers.setdefault(current, {})
            continue
        if re.match(r"^\s*\[", line):
            current = None
            continue
        if current is None:
            continue
        url = re.match(r"""^\s*url\s*=\s*(?:"([^"\\]*)"|'([^']*)')""", line)
        if url:
            servers[current]["url"] = url.group(1) if url.group(1) is not None else url.group(2)
        elif re.match(r"^\s*command\s*=", line):
            servers[current]["command"] = "?"
        elif re.match(r"^\s*enabled\s*=\s*false\b", line):
            servers[current]["enabled"] = False
    if any(host in raw for host in COSMOS_GATEWAY_HOSTS) and not any(
        is_cosmos_url(item.get("url")) for item in servers.values()
    ):
        return None
    return servers


def cosmos_mcp_status(raw: str) -> str:
    """ok | disabled | custom | missing | unreadable (same rules as the resident updater)."""
    if tomllib is not None:
        try:
            data = tomllib.loads(raw)
        except Exception:  # noqa: BLE001 - any parse error is "unreadable".
            return "unreadable"
        servers_value = data.get("mcp_servers", {})
        if not isinstance(servers_value, dict):
            return "unreadable"
        servers = {str(k): v for k, v in servers_value.items() if isinstance(v, dict)}
    else:
        scanned = _scan_servers(raw)
        if scanned is None:
            return "unreadable"
        servers = scanned
    disabled = False
    for value in servers.values():
        if is_cosmos_url(value.get("url")):
            if value.get("enabled", True) is False:
                disabled = True
            else:
                return "ok"
    if disabled:
        return "disabled"
    if COSMOS_MCP_NAME in servers:
        return "custom"
    return "missing"


def current_status() -> str:
    path = codex_config_path()
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return "missing"
    except (OSError, UnicodeDecodeError):
        return "unreadable"
    return cosmos_mcp_status(raw)


def _state_path() -> Path:
    return state_dir() / STATE_FILE


def last_reported_status() -> str:
    try:
        state = json.loads(_state_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return "ok"
    status = state.get("status") if isinstance(state, dict) else None
    return status if isinstance(status, str) else "ok"


def report_cosmos_health(payload: JsonDict) -> JsonDict | None:
    """Queue one event when the Cosmos status changes; otherwise do nothing."""
    status = current_status()
    previous = last_reported_status()
    if status == previous:
        return None
    if status == "ok" and previous not in PROBLEM_STATUSES:
        return None
    if not should_capture_payload(payload):
        return None  # Report from the next E3 session instead.
    event_type = "cosmos_mcp_restored" if status == "ok" else f"cosmos_mcp_{status}"
    event = capture_metadata_event(
        payload,
        hook_event="SessionStart",
        event_type=event_type,
        event_metadata={"cosmos_mcp_status": status, "cosmos_mcp_previous_status": previous},
    )
    write_json_atomic(_state_path(), {"status": status})
    return event
