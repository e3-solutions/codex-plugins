"""Decision-time Forum cue for E3 repositories; never searches or stores prompt text.

Agents that received relevant Forum lessons mostly read past them, and searches
ran after the approach was already chosen. On the first prompt of a thread (and
on a long new task at most once an hour) this asks for one targeted Forum search
before committing to an approach, plus a one-line USE/SKIP/NONE decision that
makes Forum yield measurable from ordinary assistant messages.
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
    "to an approach. Use 2-4 plain words naming the mechanism or failure (e.g. \"latency timeout\", "
    "\"retry idempotency\"); do not include function, file, table or ticket names, repo names, or a "
    "post title you already know. If it returns nothing, retry once with fewer words and "
    "mode=\"related\". For each returned post you read, write one line exactly like "
    "`Forum: USE <post_id> - <how it changes or confirms the plan>` or "
    "`Forum: SKIP <post_id> - <why it does not apply>`. If nothing relevant came back, write "
    "`Forum: NONE - <query>`. Then continue normally. Skip this for status questions, approvals "
    "and short follow-ups."
)

_E3_REMOTE = re.compile(
    r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"e3-solutions/[A-Za-z0-9_.-]+",
    re.IGNORECASE,
)


def state_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "forum-cue"


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
