"""Keep Sesh searches out of automated runs and cap them per user turn.

A Sep 24 - Oct 1 yield study graded 65 Sesh searches made by automated runs
(Codex suggestion generators, heartbeat automations, Linear Progress Sync and
other template workers). None of them helped. This module decides whether a
run is automated and enforces a per-turn search cap from the PreToolUse hook.

Signals, strongest first:
1. E3_AUTOMATED_AGENT=1 in the environment. Workers we launch set it (the
   Linear Progress Sync `codex exec` worker does).
2. A Codex `exec` session (session_meta originator `codex_exec` or source
   `exec`) or a Codex approval-review subagent (`guardian_review`), read from
   the rollout header when Codex wrote one (`--ephemeral` runs write none).
3. A machine-written prompt: Codex heartbeat automations wrap their prompt in
   `<heartbeat>`/`<automation_id>`; the Codex suggestion generator, the Linear
   Progress Sync worker and the Claude Code codex plugin's review gate start
   with fixed templates. Prompt matching only
   applies to the turn that carries the prompt (a heartbeat turn inside a
   person's thread does not mark the whole thread).

State is one small JSON file per session under ~/.codex/sesh-gate/sessions.
It holds the reason, the current turn key and a search count, never prompt
text or queries.
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

AUTOMATED_ENV = "E3_AUTOMATED_AGENT"
GATE_ENV = "E3_SESH_SEARCH_GATE_ENABLED"
MAX_SEARCHES_ENV = "E3_SESH_MAX_SEARCHES_PER_TURN"
DEFAULT_MAX_SEARCHES_PER_TURN = 2

SESH_SEARCH_SUFFIX = "sesh__search_coding_sessions"

_OFF = {"0", "false", "no", "off"}
_ON = {"1", "true", "yes", "on"}

# Fixed openings of machine-written prompts. Matched only near the start.
_PROMPT_TEMPLATES = (
    ("heartbeat_automation", re.compile(r"\A\s*<heartbeat>|<automation_id>")),
    ("suggestion_generator", re.compile(
        r"\A\s*#\s*Overview\s+Generate \d+ to \d+ hyperpersonalized suggestions")),
    ("linear_progress_sync", re.compile(
        r"\A\s*You are Linear Progress Sync running inside Codex\.")),
    # Claude Code's codex plugin: stop-time review gate and adversarial review.
    ("review_gate", re.compile(
        r"\A\s*<task>\s*Run a stop-gate review of the previous Claude turn\.")),
    ("review_gate", re.compile(
        r"\A\s*<role>\s*You are Codex performing an adversarial software review\.")),
)
_PROMPT_SCAN_CHARS = 400

_EXEC_ORIGINATORS = {"codex_exec"}
_AUTOMATED_THREAD_SOURCES = {"guardian_review"}


def _flag(name: str, default: str) -> str:
    return os.environ.get(name, default).strip().lower()


def gate_enabled() -> bool:
    return _flag(GATE_ENV, "1") not in _OFF


def max_searches_per_turn() -> int | None:
    """Per-turn Sesh search cap, or None when uncapped or the gate is off."""
    if not gate_enabled():
        return None
    raw = _flag(MAX_SEARCHES_ENV, str(DEFAULT_MAX_SEARCHES_PER_TURN))
    if raw in _OFF:
        return None
    try:
        value = int(raw)
    except ValueError:
        return DEFAULT_MAX_SEARCHES_PER_TURN
    return value if value > 0 else None


def env_automation_reason() -> str | None:
    return "env" if _flag(AUTOMATED_ENV, "0") in _ON else None


def prompt_automation_reason(prompt) -> str | None:
    if not isinstance(prompt, str):
        return None
    head = prompt[:_PROMPT_SCAN_CHARS]
    for reason, pattern in _PROMPT_TEMPLATES:
        if pattern.search(head):
            return reason
    return None


def transcript_automation_reason(transcript_path) -> str | None:
    """Read only the rollout's first line (session_meta) when Codex wrote one."""
    if not isinstance(transcript_path, str) or not transcript_path:
        return None
    try:
        with Path(transcript_path).expanduser().open("rb") as handle:
            line = handle.readline(262144)
        record = json.loads(line)
    except (OSError, ValueError):
        return None
    meta = record.get("payload") if isinstance(record, dict) else None
    if not isinstance(meta, dict):
        return None
    if str(meta.get("originator") or "").strip().lower() in _EXEC_ORIGINATORS:
        return "codex_exec"
    source = meta.get("source")
    if isinstance(source, str) and source.strip().lower() == "exec":
        return "codex_exec"
    if str(meta.get("thread_source") or "").strip().lower() in _AUTOMATED_THREAD_SOURCES:
        return "review_subagent"
    subagent = source.get("subagent") if isinstance(source, dict) else None
    if isinstance(subagent, dict) and str(subagent.get("other") or "").lower() == "guardian":
        return "review_subagent"
    return None


def session_automation_reason(payload) -> str | None:
    """Signals that hold for the whole session (env or rollout header)."""
    if not isinstance(payload, dict):
        return env_automation_reason()
    return env_automation_reason() or transcript_automation_reason(
        payload.get("transcript_path") or payload.get("transcriptPath"))


# ---------------------------------------------------------------- state ---

def state_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sesh-gate"


def _session_key(payload) -> str | None:
    if not isinstance(payload, dict):
        return None
    session = payload.get("session_id") or payload.get("sessionId")
    if not isinstance(session, str) or not session or "/" in session or session.startswith("."):
        return None
    return session


def _state_path(session: str, directory: Path | None) -> Path:
    return (directory or state_dir()) / "sessions" / f"{session}.json"


def load_state(payload, *, directory: Path | None = None) -> dict:
    session = _session_key(payload)
    if not session:
        return {}
    try:
        loaded = json.loads(_state_path(session, directory).read_text())
    except (OSError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def save_state(payload, state: dict, *, directory: Path | None = None) -> None:
    session = _session_key(payload)
    if not session:
        return
    path = _state_path(session, directory)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_suffix(f".{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, sort_keys=True))
    os.replace(tmp, path)


def _turn_key(payload, state: dict) -> str | None:
    turn = payload.get("turn_id") or payload.get("turnId") if isinstance(payload, dict) else None
    return turn if isinstance(turn, str) and turn else state.get("turn")


def start_turn(payload, *, directory: Path | None = None, now: float | None = None) -> dict:
    """Record a new user turn from UserPromptSubmit and classify it."""
    state = load_state(payload, directory=directory)
    turn = _turn_key(payload, {}) or f"t{(time.time() if now is None else now):.6f}"
    session_reason = state.get("session_automated") or session_automation_reason(payload)
    turn_reason = prompt_automation_reason(payload.get("prompt") if isinstance(payload, dict) else None)
    state.update({
        "turn": turn,
        "searches": 0,
        "session_automated": session_reason,
        "turn_automated": turn_reason,
    })
    save_state(payload, state, directory=directory)
    return state


def automation_reason(payload, state: dict | None = None) -> str | None:
    """Why the current turn is automated, or None for a person's turn."""
    state = load_state(payload) if state is None else state
    return (session_automation_reason(payload)
            or state.get("session_automated")
            or state.get("turn_automated"))


def is_sesh_search(payload) -> bool:
    if not isinstance(payload, dict):
        return False
    name = payload.get("tool_name") or payload.get("toolName") or payload.get("name")
    return isinstance(name, str) and name.lower().endswith(SESH_SEARCH_SUFFIX)


def check_sesh_search(payload, *, directory: Path | None = None) -> str | None:
    """Return a block message for this Sesh search, or None to allow it.

    Counts allowed searches per turn. Never raises.
    """
    if not gate_enabled() or not is_sesh_search(payload):
        return None
    try:
        state = load_state(payload, directory=directory)
        turn = _turn_key(payload, state)
        if turn and turn != state.get("turn"):
            # A turn we did not see start (e.g. hook installed mid-session).
            state.update({"turn": turn, "searches": 0, "turn_automated": None})
        reason = automation_reason(payload, state)
        if reason:
            return (
                "Sesh search is turned off for automated runs "
                f"({reason}). Continue the task without it."
            )
        cap = max_searches_per_turn()
        count = int(state.get("searches") or 0)
        if cap is not None and count >= cap:
            return (
                f"Sesh search limit reached: {cap} searches per user turn. "
                "Use the results you already have; search again only in a later "
                "turn if the user asks."
            )
        state["searches"] = count + 1
        save_state(payload, state, directory=directory)
    except Exception:  # noqa: BLE001 - the gate must never break a tool call.
        return None
    return None
