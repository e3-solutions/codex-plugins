"""COR-4911: one Forum search at the decision point, not just on the first prompt.

Why: in the Oct 16 smoke test the lesson helped once it reached the agent (pasted in: 16/29 tasks
done vs 9-11/29 without Forum), but Forum search did not get it there. 13 of 30 runs never
searched, and 8 of the 17 that did used case names (customer, branch or function names) that
missed, while the mechanism words the original agents used ("release branch ancestry") found the
post on page 1 in 10 of 10 tasks. Searching from the request text failed too: the request rarely
names the mechanism. The agent knows it only after reading code. The prompt-time cue
(forum_cue.py) fires before that.

So once per turn, at the agent's first edit or its 6th tool call, whichever comes first, this
adds one note asking for ONE Forum search naming the general mechanism. At most 3 per session.
50% of sessions (by session-id hash) are a holdout: the moment is logged as withheld and nothing
is shown, so live yield can be compared in the same weeks.

COR-5034 (Oct 28, 2026): Forum is hidden from Cosmos, so this cue is off by default and logs
nothing. FORUM_DECISION_CUE=1 (or any value but 0/false/no/off) turns it back on.

Byte-identical in codex-session-logging and claude-session-logging (tests pin it); each plugin's
forum_cue supplies the E3-repository rule and the state location. Logs only content-free events
(forum_decision_cue_shown / _withheld with the reason, index and Forum searches so far in the
turn); local state holds counters only, never prompt, path or query text.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path

ENV = "FORUM_DECISION_CUE"
DEFAULT = "0"  # COR-5034: off unless FORUM_DECISION_CUE is set on.
HOLDOUT_ID = "forum_decision_cue"
VERSION = "2026-10-21"
FIRE_AT_CALL = 6
MAX_PER_SESSION = 3
EDIT_TOOLS = frozenset({"apply_patch", "Edit", "Write", "MultiEdit", "NotebookEdit"})
CUE = (
    "Forum check (E3 Collective): before you commit to a cause, fix or design, run ONE "
    "forum__search_research_posts (Cosmos MCP) with 3-8 words naming the general mechanism you "
    "are dealing with, in your own words (e.g. \"OCR ensemble evaluation\", \"SMS receipt auth "
    "failure\"). Do not use ticket IDs, customer, project or branch names, file paths or function "
    "names: Forum posts are written about general mechanisms, so those words miss. If you already "
    "searched with case names, search once more this way. If a post fits, check it against the "
    "code and use it; if none fits, say so in one line and continue."
)


def enabled(env=None) -> bool:
    value = (os.environ if env is None else env).get(ENV, DEFAULT)
    return value.strip().lower() not in {"0", "false", "no", "off"}


def held_out(session_id: str) -> bool:
    """50% holdout from the session id alone, so every tool and analyst agrees."""
    digest = hashlib.sha256(f"{HOLDOUT_ID}:{session_id.strip().lower()}".encode("utf-8")).digest()
    return bool(digest[0] & 1)


def _session(payload) -> str | None:
    session = payload.get("session_id") if isinstance(payload, dict) else None
    if not isinstance(session, str) or not session or "/" in session or session.startswith("."):
        return None
    return session


def _path(session: str, directory: Path | None) -> Path:
    if directory is None:
        from forum_cue import state_dir

        directory = state_dir()
    return Path(directory) / "decision" / session


def _update(session: str, directory: Path | None, change):
    """Run change(state) under a lock and save it; returns change's result."""
    path = _path(session, directory)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path.with_suffix(".lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            state = json.loads(path.read_text())
        except (OSError, ValueError):
            state = {}
        result = change(state)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(state))
        os.replace(tmp, path)
        return result


def new_turn(payload, *, directory: Path | None = None) -> None:
    """UserPromptSubmit: the next tool calls belong to a new turn."""
    session = _session(payload)
    if not session or not enabled() or not _path(session, directory).exists():
        return  # nothing counted yet for this session, so nothing to reset

    def reset(state):
        state.update(calls=0, forum_searches=0, fired=False)

    _update(session, directory, reset)


def decision_cue(payload, *, directory: Path | None = None) -> tuple[str | None, dict | None]:
    """PreToolUse: (hook JSON or None, exposure event or None). Never raises past the caller's guard."""
    if not enabled() or not isinstance(payload, dict):
        return None, None
    if payload.get("hook_event_name", "PreToolUse") != "PreToolUse":
        return None, None
    session, cwd = _session(payload), payload.get("cwd")
    tool = payload.get("tool_name")
    if not session or not isinstance(cwd, str) or not cwd or not isinstance(tool, str):
        return None, None

    def step(state):
        if "e3" not in state:  # one git call per session, not per tool call
            from forum_cue import _e3_repository

            state["e3"] = bool(_e3_repository(cwd))
        if not state["e3"]:
            return None
        state["calls"] = state.get("calls", 0) + 1
        forum_tool = "search_research_posts" in tool or "forum__" in tool
        if "search_research_posts" in tool:
            state["forum_searches"] = state.get("forum_searches", 0) + 1
        if state.get("fired") or forum_tool or state.get("fires", 0) >= MAX_PER_SESSION:
            return None
        if tool in EDIT_TOOLS:
            reason = "first_edit"
        elif state["calls"] >= FIRE_AT_CALL:
            reason = f"call_{FIRE_AT_CALL}"
        else:
            return None
        state["fired"] = True
        state["fires"] = state.get("fires", 0) + 1
        return reason, state["fires"], state.get("forum_searches", 0)

    fired = _update(session, directory, step)
    if not fired:
        return None, None
    reason, index, searches = fired
    withheld = held_out(session)
    event = {"event_type": f"forum_decision_cue_{'withheld' if withheld else 'shown'}",
             "metadata": {"forum_decision_cue": "withheld" if withheld else "shown",
                          "forum_decision_cue_reason": reason, "forum_decision_cue_index": index,
                          "forum_searches_in_turn_before": searches,
                          "forum_decision_cue_version": VERSION}}
    if withheld:
        return None, event
    output = json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": CUE}})
    return output, event


def merge_outputs(first: str | None, second: str | None) -> str | None:
    """Join two PreToolUse hook outputs that each carry additionalContext into one."""
    if not first or not second:
        return first or second
    try:
        a, b = json.loads(first), json.loads(second)
        text = "\n\n".join(x["hookSpecificOutput"]["additionalContext"] for x in (a, b))
        a["hookSpecificOutput"]["additionalContext"] = text
        return json.dumps(a)
    except (ValueError, KeyError, TypeError):
        return first
