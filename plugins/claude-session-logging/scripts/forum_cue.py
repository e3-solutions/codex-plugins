"""Decision-time Forum cue for E3 repositories; never searches or stores prompt text.

Claude Code copy of plugins/codex-session-logging/scripts/forum_cue.py. Only state_dir()
differs; tests keep the cue text and gating identical.

Agents that received relevant Forum lessons mostly read past them, and searches
ran after the approach was already chosen. On the first prompt of a thread (and
on a long new task at most once an hour) this asks for one targeted Forum search
before committing to an approach, then one explained assessment per post read,
passing the search's search_id so Forum can tie each assessment to its search.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from pathlib import Path

ENABLED_ENV = "FORUM_CUE_ENABLED"
MIN_GAP_SECONDS = 3600
NEW_TASK_MIN_WORDS = 25

CUE = (
    "Forum check (E3 Collective): if this message asks you to decide how to build, fix, "
    "evaluate or investigate something, run ONE forum__search_research_posts before you commit "
    "to an approach. Use 2-4 words naming the system, tool or component plus the failure or "
    "decision (e.g. \"OCR ensemble evaluation\", \"SMS receipt auth failure\", \"release branch "
    "ancestry\"); do not include ticket IDs, file paths, function names, or a post title you already "
    "know. Forum holds internal lessons, not vendor capability facts. Results already contain each "
    "full post and its body_sha256, plus a search_id for the search. For each post you fully read, "
    "before completing the task call give_collective_feedback once with post_id and body_sha256 "
    "from the result, the search_id from that search, an explanation, and kind=up if useful and "
    "supported, kind=down if evidence is incorrect or misleading, or kind=abstain if evidence is "
    "insufficient or the post is not applicable. No sentiment is forced. Concern is optional and "
    "separate. No separate read, guide or status call is needed, and mutation_id is optional (pass "
    "your own only for an exact retry). If saving definitively fails, say the feedback remains "
    "unsaved. In your final answer, name the Forum posts you used. Skip the search for status "
    "questions, approvals and short follow-ups."
)

_E3_REMOTE = re.compile(
    r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"e3-solutions/[A-Za-z0-9_.-]+",
    re.IGNORECASE,
)


def state_dir() -> Path:
    # Same location rule as session_logging.state_dir(): ~/.claude/session-logging by default.
    override = os.environ.get("CLAUDE_SESSION_LOG_STATE_DIR")
    base = Path(override).expanduser() if override else Path.home() / ".claude" / "session-logging"
    return base / "forum-cue"


def _enabled() -> bool:
    return os.environ.get(ENABLED_ENV, "1").strip().lower() not in {"0", "false", "no", "off"}


def _e3_repository(cwd: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True, text=True, check=False, timeout=0.5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0 and bool(_E3_REMOTE.fullmatch(result.stdout.strip()))


def forum_cue(payload, *, directory: Path | None = None, now: float | None = None) -> str | None:
    """Return hook JSON carrying the cue, or None. Records only session id, time and word count."""
    if not _enabled() or not isinstance(payload, dict):
        return None
    if payload.get("hook_event_name", "UserPromptSubmit") != "UserPromptSubmit":
        return None
    session, cwd, prompt = payload.get("session_id"), payload.get("cwd"), payload.get("prompt")
    if not isinstance(session, str) or not session or "/" in session or session.startswith("."):
        return None
    if not isinstance(cwd, str) or not cwd or not _e3_repository(cwd):
        return None
    words = len(prompt.split()) if isinstance(prompt, str) else 0
    now = time.time() if now is None else now
    directory = directory or state_dir()
    marker = directory / "sessions" / session
    if marker.exists():
        try:
            last = float(marker.read_text() or 0)
        except (OSError, ValueError):
            last = 0.0
        if words < NEW_TASK_MIN_WORDS or now - last < MIN_GAP_SECONDS:
            return None
        reason = "new_task"
    else:
        reason = "first_prompt"
    marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker.write_text(str(now))
    with (directory / "cue-log.jsonl").open("a") as log:
        log.write(json.dumps({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "session_id": session, "reason": reason, "prompt_words": words,
        }) + "\n")
    return json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit", "additionalContext": CUE,
    }})
