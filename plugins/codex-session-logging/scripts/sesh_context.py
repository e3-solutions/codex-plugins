"""Local-only Sesh boundary guidance; never performs a search or stores a query.

The cue goes out with a new thread's first prompt (not at bare startup, when the
hook cannot yet tell a person's task from a machine template) and again after
compaction. Automated runs (see sesh_gate) never get it.
"""
import os
import re
import subprocess
import sys

try:
    import sesh_gate
except ImportError:  # Loaded by file path (tests, Jolly Roger) without the script dir.
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import sesh_gate

CONTEXT = (
    "Sesh prior-work context: For this coding task (and again after compaction), "
    "make one relevant Sesh coding-session search when the tool is available. "
    "Make at most {cap} Sesh searches per user turn; further searches in the same "
    "turn are blocked. Respect an explicit opt-out. Do not repeat a query already "
    "attempted for this context boundary or retry a failed search unless the user "
    "asks. If the tool is missing "
    "or search fails, report that briefly and continue the task with available "
    "evidence; do not block work or change access. Read the search-coding-sessions "
    "skill and judge the actual returned passages, not titles or similarity. "
    "Repository filters require exact indexed values, not guessed folder names "
    "or remote aliases; preserve explicitly requested scope and do not silently "
    "drop a filter after no matches. "
    "Open supporting sources using the exact source arguments returned by search "
    "when verification is needed. Distinguish supported, partial, and unsupported "
    "results. Do not claim a saved receipt without verification. This hook does "
    "not capture question text or grant access; existing privacy controls remain. "
    "Never send Slack messages as part of this workflow."
)


def context_text():
    cap = sesh_gate.max_searches_per_turn()
    if cap is None:
        return CONTEXT.replace(
            "Make at most {cap} Sesh searches per user turn; further searches in the "
            "same turn are blocked. ", "")
    return CONTEXT.replace("{cap}", str(cap))


def _cue_disabled():
    return any(os.environ.get(name, "1").strip().lower() in {
        "0", "false", "no", "off"
    } for name in ("E3_SESH_CONTEXT_ENABLED", "E3_SESH_START_SEARCH_ENABLED"))


def _e3_repository(cwd):
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True, text=True, check=False, timeout=0.5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and bool(re.fullmatch(
        r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
        r"e3-solutions/[A-Za-z0-9_.-]+", result.stdout.strip(), re.IGNORECASE
    ))


def sesh_context(payload, *, directory=None):
    """SessionStart: cue after compaction; defer a startup cue to the first prompt."""
    if _cue_disabled() or sesh_gate.env_automation_reason():
        return None
    if not isinstance(payload, dict):
        return None
    if payload.get("hook_event_name", "SessionStart") != "SessionStart":
        return None
    source = payload.get("source")
    if source not in {"startup", "compact"}:
        return None
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return None
    if not _e3_repository(cwd):
        return None
    if source == "startup":
        if sesh_gate.session_automation_reason(payload):
            return None
        state = sesh_gate.load_state(payload, directory=directory)
        state["cue_pending"] = True
        sesh_gate.save_state(payload, state, directory=directory)
        return None
    if sesh_gate.automation_reason(payload, sesh_gate.load_state(payload, directory=directory)):
        return None
    return context_text()


def sesh_prompt_context(payload, state, *, directory=None):
    """UserPromptSubmit: deliver a deferred startup cue unless the turn is automated.

    `state` is the gate state returned by sesh_gate.start_turn for this prompt.
    """
    if not isinstance(state, dict) or not state.get("cue_pending"):
        return None
    state["cue_pending"] = False
    sesh_gate.save_state(payload, state, directory=directory)
    if _cue_disabled() or sesh_gate.automation_reason(payload, state):
        return None
    return context_text()
