"""COR-4887 hook glue shared byte-for-byte by codex-session-logging and the standalone sesh plugin.

- sesh_event_metadata: labels added to every Sesh tool event (sesh_arm, sesh_routine_arm and,
  for searches, the scope enum the agent asked for). Never the query or any other tool input.
- snapshot_metadata: labels added to every environment snapshot (SessionStart).
- nudge_event / capture_nudge: one content-free exposure event per cue moment, shown or withheld:
  sesh_startup_cue_*, sesh_past_work_cue_*, sesh_plan_cue_* (the last named as in claude-sesh).
  sesh_arm is the assignment and these events are the exposure, so the holdout analysis can
  report both. The event type alone already says shown/withheld, so it survives an ingest that
  drops unknown labels.
Every function fails open to "no labels".
"""
from __future__ import annotations

SCOPES = frozenset({"all", "mine", "team"})


def is_sesh_tool(name) -> bool:
    """Every Sesh tool and the source-open tools its results point to (same rule as sesh_plugin)."""
    if not isinstance(name, str):
        return False
    from session_logging import SOURCE_OPEN_TOOLS, is_sesh_search_tool

    return is_sesh_search_tool(name) or name in SOURCE_OPEN_TOOLS or "sesh__" in name


def _arm(payload) -> dict:
    try:
        from sesh_context import arm_metadata
        from session_logging import PLUGIN_VERSION

        return {**arm_metadata(payload), "sesh_plugin_version": PLUGIN_VERSION}
    except Exception:  # noqa: BLE001
        return {}


def sesh_event_metadata(payload) -> dict:
    """Labels for a Sesh tool's started/finished event; {} for any other tool."""
    try:
        if not isinstance(payload, dict) or not is_sesh_tool(payload.get("tool_name")):
            return {}
        metadata = _arm(payload)
        from session_logging import is_sesh_search_tool

        tool_input = payload.get("tool_input")
        if is_sesh_search_tool(payload.get("tool_name")) and isinstance(tool_input, dict):
            scope = tool_input.get("scope")
            if isinstance(scope, str) and scope in SCOPES:
                metadata["sesh_scope"] = scope
        return metadata
    except Exception:  # noqa: BLE001
        return {}


def snapshot_metadata(payload, experiment=None) -> dict:
    """The COR-4681 experiment labels (when on) plus this session's COR-4887 arm."""
    metadata = dict(experiment or {})
    metadata.update(_arm(payload))
    return metadata


def nudge_event(payload, nudge_metadata) -> dict:
    """Metadata for an exposure event: the moment's labels, the arm and the trigger tool's name."""
    metadata = dict(nudge_metadata)
    metadata.update(_arm(payload))
    tool = payload.get("tool_name") if isinstance(payload, dict) else None
    if isinstance(tool, str) and len(tool) <= 256:
        metadata["tool_name"] = tool
    return metadata


CUE_KEYS = ("sesh_plan_cue", "sesh_startup_cue", "sesh_past_work_cue")


def nudge_event_type(nudge_metadata) -> str | None:
    for key in CUE_KEYS:
        value = nudge_metadata.get(key) if isinstance(nudge_metadata, dict) else None
        if value in {"shown", "withheld"}:
            return f"{key}_{value}"
    return None


def capture_nudge(payload, nudge_metadata, hook_event) -> None:
    """Queue the exposure event through this plugin's own logger (same capture rules as every event)."""
    from session_logging import capture_metadata_event, should_capture_payload

    event_type = nudge_event_type(nudge_metadata)
    if event_type and should_capture_payload(payload):
        capture_metadata_event(payload, hook_event=hook_event, event_type=event_type,
                               event_metadata=nudge_event(payload, nudge_metadata))
