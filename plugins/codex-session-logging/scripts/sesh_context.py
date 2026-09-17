"""Local-only Sesh boundary guidance; never performs a search or stores a query."""
import os
import re
import subprocess

CONTEXT = (
    "Sesh prior-work context: At chat start and after compaction, use the current "
    "coding task to make one relevant Sesh coding-session search when the tool is "
    "available. At startup with no task yet, wait for the user's task. Respect an "
    "explicit opt-out. Do not repeat a query already attempted for this context "
    "boundary or retry a failed search unless the user asks. If the tool is missing "
    "or search fails, report that briefly and continue the task with available "
    "evidence; do not block work or change access. Read the search-coding-sessions "
    "skill and judge the actual returned passages, not titles or similarity. "
    "Open supporting sources using the exact source arguments returned by search "
    "when verification is needed. Distinguish supported, partial, and unsupported "
    "results. Do not claim a saved receipt without verification. This hook does "
    "not capture question text or grant access; existing privacy controls remain. "
    "Never send Slack messages as part of this workflow."
)


def sesh_context(payload):
    if any(os.environ.get(name, "1").strip().lower() in {
        "0", "false", "no", "off"
    } for name in ("E3_SESH_CONTEXT_ENABLED", "E3_SESH_START_SEARCH_ENABLED")):
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("hook_event_name", "SessionStart") != "SessionStart":
        return None
    if payload.get("source") not in {"startup", "compact"}:
        return None
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return None
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True, text=True, check=False, timeout=0.5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0 or not re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
        r"e3-solutions/[A-Za-z0-9_.-]+", result.stdout.strip(), re.IGNORECASE
    ):
        return None
    return CONTEXT
