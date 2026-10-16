"""COR-4811: fire Sesh on need. Two switches, both off unless set to 1/true/on.

E3_SESH_ONE_PER_TURN (PreToolUse): allow one Sesh coding-session search per turn and deny
the next one in the same turn. In the Oct 16, 2026 study a second search in the same turn
helped 0 of 51 times. Opening, find-similar and ask_sesh are never blocked. Bots and
E3_SESH_EXPERIMENT_OPTOUT keep unlimited searches. Any error allows the call.

E3_SESH_PAST_WORK_CUE (UserPromptSubmit): when a person's own message in an e3-solutions
repository asks about earlier work, add a one-line cue to search Sesh once. The pattern is
the study's tight one (about 73% precise on the prompts it was built from, against 26% for a
plain past-work regex on messages people typed). Nothing is logged or stored for it.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

from sesh_context import DISABLE_ENVS, _e3_repository, _falsy, _truthy, automated_thread, opted_out, transcript_header
from session_logging import is_sesh_search_tool

ONE_PER_TURN_ENV = "E3_SESH_ONE_PER_TURN"
PAST_WORK_CUE_ENV = "E3_SESH_PAST_WORK_CUE"

DENY_REASON = (
    "Sesh: this turn already ran a coding-session search, and a second search in the same "
    "turn has not helped before. Use the results you already have, or open one with "
    "open_session_passages. You can search again in the next turn."
)
# Added to this call's existing tool_call_started event so a denial can be counted.
DENIED_METADATA = {"sesh_turn_guard": "denied"}

PAST_WORK_CUE = (
    "Sesh: this message asks about earlier work. Make one Sesh coding-session search for it "
    "before answering, and open the best result if it fits."
)
MAX_PROMPT_CHARS = 2500
# Postgres \m and \M (word start/end) are \b here; the study matched case-insensitively.
_PAST_WORK = re.compile(
    r"(\bi remember (adding|doing|fixing|setting|changing)|\bwe previously|\bwe used to|previously we"
    r"|used to have|which (change|commit|pr)\b|what (testing|tests) did we"
    r"|what we (changed|did and why|had to change)|before we (changed|moved|switched)"
    r"|when we were (sending|using|running)|'s (sessions|threads|chats)|codex://threads"
    r"|threads on my machine|changes we('ve| have) made)",
    re.IGNORECASE,
)
# Receipts, relays and pasted instructions that reuse past-tense wording.
_NOT_A_PERSON_ASKING = re.compile(
    r"(receipt|sha-?256|<instructions>|question_reply|\bA/B\b|no (db|sql)[ /-]?writes)",
    re.IGNORECASE,
)


def state_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sesh-turn-guard"


def _digest(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()[:32]


def _string(payload, *keys):
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def _human_thread(payload) -> bool:
    header = transcript_header(payload)
    return header is not None and not automated_thread(header)


def one_per_turn_denial(payload, env=None, directory: Path | None = None) -> str | None:
    """PreToolUse JSON that denies a second Sesh search in this turn, else None (allow)."""
    env = os.environ if env is None else env
    if not _truthy(env, ONE_PER_TURN_ENV):
        return None
    try:
        return _denial(payload, env, directory or state_dir())
    except Exception:  # noqa: BLE001 - fail open: the search goes ahead.
        return None


def _denial(payload, env, directory: Path) -> str | None:
    if not isinstance(payload, dict) or payload.get("hook_event_name", "PreToolUse") != "PreToolUse":
        return None
    if not is_sesh_search_tool(payload.get("tool_name")):
        return None
    session = _string(payload, "session_id", "sessionId")
    turn = _string(payload, "turn_id", "turnId")
    if session is None or turn is None or opted_out(env) or not _human_thread(payload):
        return None  # Bots keep Sesh; unknown turns or threads are allowed.
    folder = directory / _digest(session)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    marker = folder / _digest(turn)
    try:
        os.close(os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
    except FileExistsError:
        return json.dumps({"hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": DENY_REASON,
        }})
    for old in folder.iterdir():  # First search of a new turn: forget earlier turns.
        if old.name != marker.name:
            old.unlink(missing_ok=True)
    return None


def asks_about_past_work(prompt) -> bool:
    return (
        isinstance(prompt, str)
        and len(prompt) < MAX_PROMPT_CHARS
        and _PAST_WORK.search(prompt) is not None
        and _NOT_A_PERSON_ASKING.search(prompt) is None
    )


def past_work_cue(payload, env=None) -> str | None:
    """The one-line cue for a person's past-work question in an E3 repository, else None."""
    env = os.environ if env is None else env
    if not _truthy(env, PAST_WORK_CUE_ENV) or not isinstance(payload, dict):
        return None
    if payload.get("hook_event_name", "UserPromptSubmit") != "UserPromptSubmit":
        return None
    if not asks_about_past_work(payload.get("prompt")):
        return None
    if opted_out(env) or any(_falsy(env.get(name)) for name in DISABLE_ENVS):
        return None
    if not _human_thread(payload) or not _e3_repository(payload):
        return None
    return PAST_WORK_CUE


def with_past_work_cue(output: str | None, payload, env=None) -> str | None:
    """Add the cue to the UserPromptSubmit output (None or hook JSON); unchanged when it does not fire."""
    try:
        cue = past_work_cue(payload, env)
        if cue is None:
            return output
        if not output:
            return json.dumps({"hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit", "additionalContext": cue,
            }})
        merged = json.loads(output)
        specific = merged["hookSpecificOutput"]
        specific["additionalContext"] = f"{specific['additionalContext']}\n\n{cue}"
        return json.dumps(merged)
    except Exception:  # noqa: BLE001 - the cue must never change or break the prompt.
        return output
