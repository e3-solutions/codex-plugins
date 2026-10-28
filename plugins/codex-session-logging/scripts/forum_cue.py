"""Decision-time Forum cue for E3 repositories; never searches or stores prompt text.

Agents that received relevant Forum lessons mostly read past them, and searches
ran after the approach was already chosen. On the first prompt of a thread (and
on a long new task at most once an hour) this asks for one targeted Forum search
before committing to an approach, then one explained assessment per post read,
passing the search's search_id so Forum can tie each assessment to its search.

Off by default since COR-5034 (Forum hidden from Cosmos); FORUM_CUE_ENABLED=1 turns it on.

COR-4883: with FORUM_CUE_AB on, sessions are split by session-id hash between this cue (v1)
and two v2 cues; off by default, so v1 is unchanged.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import time
from pathlib import Path

ENABLED_ENV = "FORUM_CUE_ENABLED"
# COR-5034 (Oct 28, 2026): Forum is hidden from Cosmos, so the cue is off by default.
# FORUM_CUE_ENABLED=1 (or any value but 0/false/no/off) turns it back on.
ENABLED_DEFAULT = "0"
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

# COR-4883 cue v2. Two findings from live Forum use (Oct 1-19, 2026): agents rate every result one
# call at a time, so most rating effort is walls of abstain (2,836 abstains vs 1,778 ups; 66% of
# abstains just say "off-topic"), and 37% of first-page searches are duplicate checks run before
# sharing. v2 rates only posts the agent used or checked and caps duplicate checks. A read of 25
# sessions also suggested mid-task searches naming a concrete mechanism hit more often than
# first-minute searches, but the logs show no gap (first search 8.1% vs later 6.3% honest yield),
# so that is a separate, secondary arm (v2_midtask), not part of v2 itself.
_V2_TOOL = (
    "Forum check (E3 Collective): Forum is the tool forum__search_research_posts on the Cosmos MCP "
    "server (named cosmos or e3-cosmos); there is no separate `forum` server to look for. "
)
_V2_SEARCH_NOW = (
    "If this message asks you to decide how to build, fix, evaluate or investigate something, run "
    "ONE search before you commit to an approach. Use 2-4 words naming the system, tool or component "
    "plus the failure or decision (e.g. \"OCR ensemble evaluation\", \"SMS receipt auth failure\"); "
    "no ticket IDs, file paths, function names, or a post title you already know. "
)
_V2_SEARCH_ON_CAUSE = (
    "Do not search yet. Search once you have a specific cause, mechanism or hypothesis (usually after "
    "reading code, logs or data), before you commit to a fix or design built on it. Use 3-8 words "
    "naming that mechanism with its own nouns (e.g. \"voice notice replay state\", \"live-provider CI "
    "jobs on beta PRs\"), not words copied from this request, and no ticket IDs, file paths or "
    "function names. Search again when your hypothesis or plan changes. "
)
_V2_RATE = (
    "Forum holds internal lessons, not vendor capability facts. Results already contain each full "
    "post, its body_sha256 and the search_id. Rate only posts you used or checked against your work, "
    "not every result; leave the rest unrated. Before you finish, save those ratings in ONE "
    "give_collective_feedback call with request={\"assessments\": [...]} (up to 5 posts, nothing "
    "else beside assessments), each item with post_id and body_sha256 from the result, that search's "
    "search_id, kind and an explanation: kind=up if the post is useful and supported, kind=down "
    "if its evidence is incorrect or misleading, kind=abstain if you "
    "checked it and it does not apply (start that explanation with \"Off-topic for this query:\" when "
    "it is about something else). Before share_with_collective, run at most 2 searches in total to "
    "check for a duplicate, then share or skip. If saving definitively fails, say the feedback "
    "remains unsaved. In your final answer, name the Forum posts you used. Skip Forum for status "
    "questions, approvals, short follow-ups, and when you only hand the work to another agent."
)
CUE_V2 = _V2_TOOL + _V2_SEARCH_NOW + _V2_RATE
CUE_V2_MIDTASK = _V2_TOOL + _V2_SEARCH_ON_CAUSE + _V2_RATE
CUES = {"v1": CUE, "v2": CUE_V2, "v2_midtask": CUE_V2_MIDTASK}

# COR-4883 A/B. Off unless FORUM_CUE_AB=1/true/yes/on; then each session's arm comes from its id
# alone, so Codex and Claude Code, reruns and analysts all agree:
#   h = sha256("forum_cue_v2:" + lower(trim(session_id)))
#   arm = "v1" if h[0] & 1 == 0 else ("v2_midtask" if h[1] & 1 else "v2")
# i.e. 50% v1 (control), 25% v2, 25% v2_midtask. Primary comparison: v1 vs v2 + v2_midtask.
AB_ENV = "FORUM_CUE_AB"
AB_ID = "forum_cue_v2"
# COR-4900: opt-in non-repo directories treated as E3 (off unless set).
EXTRA_DIRS_ENV = "CODEX_SESSION_LOG_EXTRA_DIRS"

_E3_REMOTE = re.compile(
    r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)"
    r"e3-solutions/[A-Za-z0-9_.-]+",
    re.IGNORECASE,
)


def state_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "forum-cue"


def extra_dirs_file() -> Path:
    # Same location as session_logging.state_dir() / "extra-dirs".
    override = os.environ.get("CODEX_SESSION_LOG_STATE_DIR")
    if override:
        return Path(override).expanduser() / "extra-dirs"
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser() / "session-logging" / "extra-dirs"


def _enabled() -> bool:
    return os.environ.get(ENABLED_ENV, ENABLED_DEFAULT).strip().lower() not in {"0", "false", "no", "off"}


def ab_enabled() -> bool:
    return os.environ.get(AB_ENV, "").strip().lower() in {"1", "true", "yes", "on"}


def cue_arm(session_id: str) -> str:
    """COR-4883 arm from the session id alone: "v1" (today's cue), "v2" or "v2_midtask"."""
    digest = hashlib.sha256(f"{AB_ID}:{session_id.strip().lower()}".encode("utf-8")).digest()
    if not digest[0] & 1:
        return "v1"
    return "v2_midtask" if digest[1] & 1 else "v2"


def _e3_repository(cwd: str) -> bool:
    try:
        result = subprocess.run(
            ["git", "-C", cwd, "remote", "get-url", "origin"],
            capture_output=True, text=True, check=False, timeout=0.5,
        )
    except (OSError, subprocess.TimeoutExpired):
        # A slow git walk outside any repo must not hide an opted-in directory;
        # this is a guidance hint only (capture checks the origin without a timeout).
        return _in_extra_dir(cwd)
    remote = result.stdout.strip() if result.returncode == 0 else ""
    if remote:
        return bool(_E3_REMOTE.fullmatch(remote))
    return _in_extra_dir(cwd)


def _in_extra_dir(cwd: str) -> bool:
    """COR-4900: a cwd with no origin under an opted-in extra directory counts as E3.

    Same rule as session_logging.allowlisted_dirs(): env var paths (os.pathsep-separated)
    plus one path per line in the extra-dirs file; relative paths, / and $HOME are ignored.
    """
    entries = os.environ.get(EXTRA_DIRS_ENV, "").split(os.pathsep)
    try:
        entries += extra_dirs_file().read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        pass
    home = os.path.realpath(Path.home())
    target = Path(os.path.realpath(Path(cwd).expanduser()))
    for entry in entries:
        entry = entry.strip()
        path = Path(entry).expanduser()
        if not entry or entry.startswith("#") or not path.is_absolute():
            continue
        root = Path(os.path.realpath(path))
        if root == Path(root.anchor) or str(root) == home:
            continue
        if target == root or root in target.parents:
            return True
    return False


def forum_cue(payload, *, directory: Path | None = None, now: float | None = None) -> str | None:
    """Return hook JSON carrying the cue, or None. Records only session id, time and word count."""
    return forum_cue_decision(payload, directory=directory, now=now)[0]


def forum_cue_decision(payload, *, directory: Path | None = None,
                       now: float | None = None) -> tuple[str | None, dict | None]:
    """Return (hook JSON or None, session-log metadata or None).

    Metadata is returned only while the COR-4883 A/B is on and the cue fired, so with
    FORUM_CUE_AB unset every session gets v1 exactly as before and nothing new is logged.
    """
    if not _enabled() or not isinstance(payload, dict):
        return None, None
    if payload.get("hook_event_name", "UserPromptSubmit") != "UserPromptSubmit":
        return None, None
    session, cwd, prompt = payload.get("session_id"), payload.get("cwd"), payload.get("prompt")
    if not isinstance(session, str) or not session or "/" in session or session.startswith("."):
        return None, None
    if not isinstance(cwd, str) or not cwd or not _e3_repository(cwd):
        return None, None
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
            return None, None
        reason = "new_task"
    else:
        reason = "first_prompt"
    arm = cue_arm(session) if ab_enabled() else None
    marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker.write_text(str(now))
    entry = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
        "session_id": session, "reason": reason, "prompt_words": words,
    }
    if arm:
        entry.update({"experiment": AB_ID, "arm": arm})
    with (directory / "cue-log.jsonl").open("a") as log:
        log.write(json.dumps(entry) + "\n")
    output = json.dumps({"hookSpecificOutput": {
        "hookEventName": "UserPromptSubmit", "additionalContext": CUES[arm or "v1"],
    }})
    metadata = ({"forum_cue_experiment": AB_ID, "forum_cue_arm": arm, "forum_cue_reason": reason}
                if arm else None)
    return output, metadata
