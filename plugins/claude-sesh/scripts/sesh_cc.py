"""Sesh hooks for Claude Code (COR-4888): the rules every hook script shares.

Same behaviour as the Codex `sesh` plugin (plugins/sesh, 0.1.0), adapted to Claude Code:

- SessionStart: the "Sesh prior-work context" cue in e3-solutions repositories, with the
  COR-4808 routine A/B (arm need_only gets no routine cue).
- UserPromptSubmit: the COR-4811 past-work cue, and a new turn marker for the guard below
  (Claude Code hook payloads carry no turn id, so this plugin keeps its own).
- PreToolUse on Sesh search: one coding-session search per turn; the second is denied.
  Ask, find-similar and open are never limited. Bots and opt-outs keep unlimited searches.
- PreToolUse on Write of a new code file: the COR-4887 "existing work" plan cue, at most
  twice a session and once per area.
- A stable 15% session holdout (E3_SESH_HOLDOUT_PCT) gets no cue at all and no one-search
  denial (the turn marker is still kept). Tools stay.

Every check fails open: an error never blocks a tool, and bots are never limited.
Nothing here reads, stores or sends prompts, queries, passages or file contents.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import subprocess
import time
import uuid
from pathlib import Path

PLUGIN_NAME = "sesh"
PLUGIN_VERSION = "0.1.2"  # keep equal to .claude-plugin/plugin.json

# --- switches (same names and defaults as the Codex plugin) --------------------------------------
DISABLE_ENVS = ("E3_SESH_CONTEXT_ENABLED", "E3_SESH_START_SEARCH_ENABLED")
EXPERIMENT_OPTOUT_ENV = "E3_SESH_EXPERIMENT_OPTOUT"
AFTER_COMPACTION_ENV = "E3_SESH_CUE_AFTER_COMPACTION"
ROUTINE_AB_ENV = "E3_SESH_ROUTINE_AB"
ONE_PER_TURN_ENV = "E3_SESH_ONE_PER_TURN"
PAST_WORK_CUE_ENV = "E3_SESH_PAST_WORK_CUE"
PLAN_CUE_ENV = "E3_SESH_PLAN_CUE"
HOLDOUT_PCT_ENV = "E3_SESH_HOLDOUT_PCT"
STATE_DIR_ENV = "E3_SESH_STATE_DIR"

# --- cue text (identical to plugins/sesh; tests/test_claude_sesh.py pins the parity) -------------
CONTEXT = (
    "Sesh prior-work context: Once the user's actual task is known, make one Sesh "
    "coding-session search for that task when the tool is available; with no task "
    "yet, wait for it. Before your first edit, open the top result using the exact "
    "source arguments the search returned. Respect an explicit opt-out. Do not "
    "repeat a query or retry a failed search unless the user asks. If the tool is "
    "missing or search fails, say so briefly and continue the task with available "
    "evidence; do not block work or change access. Read the search-coding-sessions "
    "skill and judge the actual returned passages, not titles or similarity. "
    "Repository filters require exact indexed values, not guessed folder names "
    "or remote aliases; preserve explicitly requested scope and do not silently "
    "drop a filter after no matches. Distinguish supported, partial, and unsupported "
    "results. Do not claim a saved receipt without verification. This hook does "
    "not capture question text or grant access; existing privacy controls remain. "
    "Never send Slack messages as part of this workflow."
)
COMPACTION_CONTEXT = (
    "Sesh prior-work context: At chat start and after compaction, use the current "
    "coding task to make one relevant Sesh coding-session search when the tool is "
    "available. At startup with no task yet, wait for the user's task. Respect an "
    "explicit opt-out. Do not repeat a query already attempted for this context "
    "boundary or retry a failed search unless the user asks. If the tool is missing "
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
PAST_WORK_CUE = (
    "Sesh: this message asks about earlier work. Make one Sesh coding-session search for it "
    "before answering, and open the best result if it fits."
)
DENY_REASON = (
    "Sesh: this plugin allows one coding-session search per turn (E3_SESH_ONE_PER_TURN), and this "
    "turn already ran one. Use or open the results you already have (open_session_passages), or "
    "search again in the next turn."
)
# Claude Code only: the one-search guard denial, kept on the tool_call_started event.
DENIED_METADATA = {"sesh_turn_guard": "denied"}
PLAN_CUE = (
    "Sesh existing-work check: you are about to create a new {kind} ({path}). Before you "
    "build it, make one Sesh coding-session search for existing or teammate work that already "
    "does this (name the system and what it does; no file paths). If a result fits, show it as "
    "an existing-work card: link, owner, date, and one line on what that work did. Then reuse "
    "or extend it, or say in one line why not. If nothing fits, say so in one line and continue."
)

# --- COR-4811 past-work pattern (identical to plugins/sesh/scripts/sesh_need.py) -----------------
MAX_PROMPT_CHARS = 2500
_PAST_WORK = re.compile(
    r"(\bi remember (adding|doing|fixing|setting|changing)|\bwe previously|\bwe used to|previously we"
    r"|used to have|which (change|commit|pr)\b|what (testing|tests) did we"
    r"|what we (changed|did and why|had to change)|before we (changed|moved|switched)"
    r"|when we were (sending|using|running)|'s (sessions|threads|chats)|codex://threads"
    r"|threads on my machine|changes we('ve| have) made)",
    re.IGNORECASE,
)
_NOT_A_PERSON_ASKING = re.compile(
    r"(receipt|sha-?256|<instructions>|question_reply|\bA/B\b|no (db|sql)[ /-]?writes)",
    re.IGNORECASE,
)

# --- arms ----------------------------------------------------------------------------------------
# COR-4808 routine A/B, same hash as Codex:
#   arm = ROUTINE_ARMS[sha256("sesh_routine_v1:" + lower(trim(id)))[0] & 1]
ROUTINE_AB_ID = "sesh_routine_v1"
ROUTINE_ARMS = ("routine", "need_only")
# COR-4887 holdout: a stable share of sessions gets no Sesh cue (startup, past-work or plan).
#   bucket = (b[0] * 256 + b[1]) % 100 with b = sha256("sesh_holdout_v1:" + lower(trim(id)))
#   holdout when bucket < E3_SESH_HOLDOUT_PCT (default 15)
#   SQL: (get_byte(d, 0) * 256 + get_byte(d, 1)) % 100 < 15,
#        d = sha256(convert_to('sesh_holdout_v1:' || lower(btrim(session_id)), 'UTF8'))
HOLDOUT_ID = "sesh_holdout_v1"
DEFAULT_HOLDOUT_PCT = 15
# sesh_arm: holdout/treated are the randomized arms; bots and opt-outs are never held out.
SESH_ARMS = ("holdout", "treated", "bot", "optout", "unassigned")

# --- plan cue ------------------------------------------------------------------------------------
PLAN_CUE_MAX_PER_SESSION = 2
CODE_SUFFIXES = frozenset({
    ".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".go", ".rs", ".rb", ".java", ".kt",
    ".swift", ".scala", ".cs", ".php", ".sh", ".bash", ".zsh", ".sql", ".ex", ".exs", ".c",
    ".cc", ".cpp", ".h", ".hpp", ".cu", ".lua", ".dart", ".vue", ".svelte",
})
_TEST_PATH = re.compile(
    r"(^|/)(tests?|__tests__|spec|specs|fixtures?|testdata|__mocks__|e2e)/"
    r"|(^|/)(test_[^/]*|[^/]*_test\.[^/]+|[^/]*\.(test|spec)\.[^/]+|conftest\.py)$",
    re.IGNORECASE,
)
_SKIP_PATH = re.compile(
    r"(^|/)(node_modules|dist|build|out|target|vendor|\.venv|venv|__pycache__|\.git|\.next|"
    r"generated|gen|tmp|temp|scratch|scratchpad)/|(^|/)\.[^/]+/",
    re.IGNORECASE,
)
_MIGRATION_PATH = re.compile(r"(^|/)(migrations?|migrate|alembic|schema)/", re.IGNORECASE)
_SERVICE_PATH = re.compile(r"(^|/)(services?|servers?|workers?|functions|lambdas?|api)/"
                           r"|(service|server|worker|daemon|handler)[^/]*$", re.IGNORECASE)
_SCRIPT_PATH = re.compile(r"(^|/)(scripts?|bin|tools|cli|jobs|cron)/", re.IGNORECASE)
TRIVIAL_NAMES = frozenset({"__init__.py", "index.ts", "index.js", "mod.rs", "__main__.py"})
# COR-4887 Oct 20 replay: one-off, review and analysis folders, and ticket- or date-named folders,
# were where the false cues landed. Shared with the Codex plan cue (plan_cue.py).
PLAN_SKIP_PATH = re.compile(
    r"(^|/)(outputs?|artifacts?|reviews?|visuali[sz]ations?|viz|analysis|analyses|investigations?|"
    r"reports?|docs?|examples?|notebooks?|evals?|benchmarks?|bench|playground|sandbox|archive|"
    r"backups?|data|logs?|debug|repro|qa|smoke|harness|seeds?|cor-\d+|[a-z]+-\d{2,}|"
    r"\d{4}-\d{2}-\d{2}[^/]*)/",
    re.IGNORECASE,
)

HUMAN_ENTRYPOINT_HINT = "CLAUDE_CODE_ENTRYPOINT"
TRANSCRIPT_HEAD_BYTES = 65536


# --- small helpers -------------------------------------------------------------------------------

def _falsy(value) -> bool:
    return value is not None and str(value).strip().lower() in {"0", "false", "no", "off"}


def _truthy(env, name) -> bool:
    return str(env.get(name, "")).strip().lower() in {"1", "true", "on", "yes"}


def switch_on(env, name) -> bool:
    """On unless the variable is 0/false/no/off."""
    return not _falsy(env.get(name))


def opted_out(env) -> bool:
    value = env.get(EXPERIMENT_OPTOUT_ENV)
    return value is not None and bool(str(value).strip()) and not _falsy(value)


def disabled(env) -> bool:
    return any(_falsy(env.get(name)) for name in DISABLE_ENVS)


def session_id(payload) -> str | None:
    if not isinstance(payload, dict):
        return None
    for key in ("session_id", "sessionId"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None


def digest(value: str, length: int = 32) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()[:length]


def state_dir(env=None) -> Path:
    """Plugin state: E3_SESH_STATE_DIR, else Claude Code's per-plugin data dir, else ~/.claude/sesh."""
    env = os.environ if env is None else env
    for name in (STATE_DIR_ENV, "CLAUDE_PLUGIN_DATA"):
        value = env.get(name)
        if value:
            return Path(value).expanduser()
    config = env.get("CLAUDE_CONFIG_DIR")
    base = Path(config).expanduser() if config else Path.home() / ".claude"
    return base / "sesh"


def hook_output(event: str, **fields) -> str:
    return json.dumps({"hookSpecificOutput": {"hookEventName": event, **fields}})


# --- who is driving this session -----------------------------------------------------------------

def transcript_entrypoint(payload) -> str | None:
    """The `entrypoint` Claude Code writes on transcript records (cli, claude-desktop, sdk-ts, ...).

    Bounded read of the first 64 KB; only that one label is kept, never message text.
    """
    path = payload.get("transcript_path") if isinstance(payload, dict) else None
    if not isinstance(path, str) or not os.path.isabs(path):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            head = stream.read(TRANSCRIPT_HEAD_BYTES)
    except OSError:
        return None
    for line in head.splitlines():
        try:
            record = json.loads(line)
        except (ValueError, UnicodeError, RecursionError):
            continue
        value = record.get("entrypoint") if isinstance(record, dict) else None
        if isinstance(value, str) and value:
            return value
    return None


def session_kind(payload, env=None) -> str:
    """"bot" for subagents and SDK/headless sessions, "human" for a person's session, else "unknown"."""
    env = os.environ if env is None else env
    if not isinstance(payload, dict):
        return "unknown"
    if payload.get("agent_id") or payload.get("agent_type"):
        return "bot"  # A subagent (or --agent) hook call; never limited.
    entrypoint = env.get(HUMAN_ENTRYPOINT_HINT) or transcript_entrypoint(payload)
    if not isinstance(entrypoint, str) or not entrypoint.strip():
        return "unknown"
    entrypoint = entrypoint.strip().lower()
    if entrypoint.startswith("sdk") or entrypoint in {"mcp", "github-action", "action"}:
        return "bot"
    return "human"


def human(payload, env=None) -> bool:
    return session_kind(payload, env) == "human"


# --- arms ----------------------------------------------------------------------------------------

def holdout_pct(env=None) -> int:
    env = os.environ if env is None else env
    raw = env.get(HOLDOUT_PCT_ENV)
    if raw is None or not str(raw).strip():
        return DEFAULT_HOLDOUT_PCT
    try:
        value = int(str(raw).strip())
    except ValueError:
        return DEFAULT_HOLDOUT_PCT
    return min(max(value, 0), 100)


def holdout_bucket(sid: str) -> int:
    raw = hashlib.sha256(f"{HOLDOUT_ID}:{str(sid).strip().lower()}".encode("utf-8")).digest()
    return (raw[0] * 256 + raw[1]) % 100


def routine_arm(sid: str) -> str:
    normalized = str(sid).strip().lower()
    raw = hashlib.sha256(f"{ROUTINE_AB_ID}:{normalized}".encode("utf-8")).digest()
    return ROUTINE_ARMS[raw[0] & 1]


def sesh_arm(payload, env=None) -> str:
    env = os.environ if env is None else env
    if opted_out(env):
        return "optout"
    if session_kind(payload, env) == "bot":
        return "bot"  # Bots keep Sesh and every cue.
    sid = session_id(payload)
    if sid is None:
        return "unassigned"
    return "holdout" if holdout_bucket(sid) < holdout_pct(env) else "treated"


def arm_metadata(payload, env=None) -> dict:
    """Labels only, logged on the environment snapshot and every Sesh event."""
    env = os.environ if env is None else env
    arm = sesh_arm(payload, env)
    metadata = {"sesh_arm": arm, "sesh_holdout_pct": holdout_pct(env),
                "sesh_plugin_version": PLUGIN_VERSION}
    sid = session_id(payload)
    if arm == "treated" and sid is not None and switch_on(env, ROUTINE_AB_ENV):
        metadata["sesh_routine_arm"] = routine_arm(sid)
    return metadata


# --- repository ----------------------------------------------------------------------------------

_E3_REMOTE = re.compile(
    r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)e3-solutions/[A-Za-z0-9_.-]+",
    re.IGNORECASE,
)


def git(cwd: str, *args: str, timeout: float | None = None) -> str | None:
    if timeout is None:  # Bounded so a slow machine skips a cue instead of stalling the hook.
        try:
            timeout = float(os.environ.get("E3_SESH_GIT_TIMEOUT_SECONDS") or 2.0)
        except ValueError:
            timeout = 2.0
    try:
        result = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                                check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return result.stdout.strip() if result.returncode == 0 else None


def e3_repository(payload) -> bool:
    cwd = payload.get("cwd") if isinstance(payload, dict) else None
    if not isinstance(cwd, str) or not cwd:
        return False
    remote = git(cwd, "remote", "get-url", "origin")
    return bool(remote) and _E3_REMOTE.fullmatch(remote) is not None


# --- SessionStart --------------------------------------------------------------------------------

def startup_cue(payload, env=None) -> str | None:
    """The startup cue, or None. Startup and /clear only (not resume); after compaction only when
    E3_SESH_CUE_AFTER_COMPACTION is on. Holdout and the routine A/B's need_only arm get none."""
    env = os.environ if env is None else env
    if not isinstance(payload, dict) or disabled(env):
        return None
    source = payload.get("source")
    compact = source == "compact"
    if source not in {"startup", "clear"} and not compact:
        return None
    if compact and not _truthy(env, AFTER_COMPACTION_ENV):
        return None
    if not e3_repository(payload):
        return None
    arm = sesh_arm(payload, env)
    if arm == "holdout":
        return None
    sid = session_id(payload)
    if (arm == "treated" and sid is not None and switch_on(env, ROUTINE_AB_ENV)
            and human(payload, env) and routine_arm(sid) == "need_only"):
        return None  # COR-4808 need_only: no routine cue; Sesh tools and logging stay.
    return COMPACTION_CONTEXT if compact else CONTEXT


# --- turns (Claude Code gives hooks no turn id) --------------------------------------------------

def _turn_file(sid: str, base: Path) -> Path:
    return base / "turns" / digest(sid)


def start_turn(payload, env=None, base: Path | None = None) -> str | None:
    """Record a new turn for this session (UserPromptSubmit). Returns the turn id."""
    sid = session_id(payload)
    if sid is None:
        return None
    base = base or state_dir(env)
    path = _turn_file(sid, base)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    turn = uuid.uuid4().hex
    tmp = path.with_name(f"{path.name}.{turn}.tmp")
    tmp.write_text(turn, encoding="utf-8")
    tmp.replace(path)
    for old in path.parent.glob(f"{path.name}.*.searched"):
        old.unlink(missing_ok=True)
    _prune_old(path.parent)
    return turn


def current_turn(payload, env=None, base: Path | None = None) -> str | None:
    sid = session_id(payload)
    if sid is None:
        return None
    try:
        value = _turn_file(sid, base or state_dir(env)).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def _search_marker(sid: str, turn: str, base: Path) -> Path:
    return base / "turns" / f"{digest(sid)}.{digest(turn, 16)}.searched"


def release_search(payload, env=None, base: Path | None = None) -> None:
    """A search that failed or was refused at the permission prompt gives the turn its search back."""
    sid, turn = session_id(payload), current_turn(payload, env, base)
    if sid is not None and turn is not None:
        _search_marker(sid, turn, base or state_dir(env)).unlink(missing_ok=True)


def searched_this_turn(payload, env=None, base: Path | None = None) -> bool:
    sid, turn = session_id(payload), current_turn(payload, env, base)
    if sid is None or turn is None:
        return False
    return _search_marker(sid, turn, base or state_dir(env)).exists()


def _prune_old(folder: Path, max_age_seconds: int = 7 * 86400) -> None:
    """Forget sessions untouched for a week (cheap; at most once an hour)."""
    stamp = folder / ".pruned"
    now = time.time()
    try:
        if now - stamp.stat().st_mtime < 3600:
            return
    except OSError:
        pass
    try:
        stamp.touch()
        for item in folder.iterdir():
            if item.name != ".pruned" and now - item.stat().st_mtime > max_age_seconds:
                item.unlink(missing_ok=True)
    except OSError:
        pass


# --- tools ---------------------------------------------------------------------------------------

# Any MCP server name, including claude.ai connectors (mcp__<uuid>__sesh__...), plugin servers
# (mcp__plugin_x_cosmos__sesh__...) and local ones (mcp__cosmos__sesh__...). Same search rule as
# the Codex plugin and the ingest (SESH_SEARCH_TOOL_PATTERN).
SESH_SEARCH_TOOL_PATTERN = re.compile(r"(?:[A-Za-z0-9_-]+__)?(?:sesh__)?search_coding_sessions")
SESH_TOOL_PATTERN = re.compile(r"mcp__[A-Za-z0-9_.-]+?__sesh__[A-Za-z0-9_]+")


def is_sesh_search_tool(name) -> bool:
    return isinstance(name, str) and SESH_SEARCH_TOOL_PATTERN.fullmatch(name) is not None


def is_sesh_tool(name) -> bool:
    return isinstance(name, str) and (
        is_sesh_search_tool(name) or SESH_TOOL_PATTERN.fullmatch(name) is not None)


# --- PreToolUse: one search per turn -------------------------------------------------------------

def one_per_turn_denial(payload, env=None, base: Path | None = None) -> str | None:
    """PreToolUse JSON denying a second Sesh search in this turn, else None (allow).

    Only human sessions are limited. Bots, opt-outs, unknown sessions and unknown turns are
    allowed, and any error allows the call.
    """
    env = os.environ if env is None else env
    try:
        if not switch_on(env, ONE_PER_TURN_ENV) or not isinstance(payload, dict):
            return None
        if not is_sesh_search_tool(payload.get("tool_name")):
            return None
        if opted_out(env) or not human(payload, env):
            return None  # Bots keep Sesh.
        sid, turn = session_id(payload), current_turn(payload, env, base)
        if sid is None or turn is None:
            return None
        marker = _search_marker(sid, turn, base or state_dir(env))
        marker.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        try:
            os.close(os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600))
        except FileExistsError:
            if sesh_arm(payload, env) == "holdout":
                return None  # COR-4887 holdout: never denied (marker kept), same as the Codex plugin.
            return hook_output("PreToolUse", permissionDecision="deny",
                               permissionDecisionReason=DENY_REASON)
        return None
    except Exception:  # noqa: BLE001 - fail open: the search goes ahead.
        return None


# --- UserPromptSubmit: past-work cue -------------------------------------------------------------

def asks_about_past_work(prompt) -> bool:
    return (
        isinstance(prompt, str)
        and len(prompt) < MAX_PROMPT_CHARS
        and _PAST_WORK.search(prompt) is not None
        and _NOT_A_PERSON_ASKING.search(prompt) is None
    )


def past_work_cue(payload, env=None) -> str | None:
    env = os.environ if env is None else env
    if not switch_on(env, PAST_WORK_CUE_ENV) or not isinstance(payload, dict):
        return None
    if not asks_about_past_work(payload.get("prompt")):
        return None
    if opted_out(env) or disabled(env) or not human(payload, env):
        return None
    if sesh_arm(payload, env) == "holdout" or not e3_repository(payload):
        return None
    return PAST_WORK_CUE


# --- PreToolUse on Write: plan cue ---------------------------------------------------------------

def plan_kind(relative: str) -> str | None:
    """What a new file at this repo-relative path would start, or None when it is not worth a cue."""
    path = relative.replace(os.sep, "/")
    name = path.rsplit("/", 1)[-1]
    suffix = os.path.splitext(name)[1].lower()
    if PLAN_SKIP_PATH.search(path):
        return None
    if name == "Dockerfile":
        return "service"
    if suffix not in CODE_SUFFIXES or name in TRIVIAL_NAMES:
        return None
    if _TEST_PATH.search(path) or _SKIP_PATH.search(path):
        return None
    if suffix == ".sql" or _MIGRATION_PATH.search(path):
        return "migration or table"
    if _SERVICE_PATH.search(path):
        return "service"
    if _SCRIPT_PATH.search(path) or suffix in {".sh", ".bash", ".zsh"}:
        return "script"
    return "module"


def plan_topic(relative: str) -> str:
    """The area a new file belongs to: its first two directories (or the file at the top level)."""
    parts = relative.replace(os.sep, "/").split("/")
    directories = parts[:-1]
    return "/".join(directories[:2]) if directories else parts[-1]


def _plan_state_path(sid: str, base: Path) -> Path:
    return base / "plan" / f"{digest(sid)}.json"


def _read_plan_state(path: Path) -> dict:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"fires": 0, "topics": []}
    if not isinstance(loaded, dict):
        return {"fires": 0, "topics": []}
    fires = loaded.get("fires") if isinstance(loaded.get("fires"), int) else 0
    topics = [t for t in loaded.get("topics", []) if isinstance(t, str)]
    return {"fires": fires, "topics": topics}


def plan_cue_decision(payload, env=None, base: Path | None = None):
    """(cue text or None, log metadata or None) for a Write about to create a new code file.

    Fires at most twice a session and once per area, in human sessions in e3-solutions
    repositories, never in a turn that already ran a Sesh search. In the holdout the moment is
    logged as "withheld" so the arms can be compared, and no cue is shown.
    """
    env = os.environ if env is None else env
    if not switch_on(env, PLAN_CUE_ENV) or not isinstance(payload, dict):
        return None, None
    if payload.get("tool_name") != "Write" or opted_out(env) or disabled(env):
        return None, None
    tool_input = payload.get("tool_input")
    file_path = tool_input.get("file_path") if isinstance(tool_input, dict) else None
    cwd = payload.get("cwd")
    if not isinstance(file_path, str) or not os.path.isabs(file_path) or not isinstance(cwd, str):
        return None, None
    if os.path.lexists(file_path):
        return None, None  # Only new files; edits and rewrites never cue.
    sid = session_id(payload)
    if sid is None or not human(payload, env):
        return None, None
    root = git(cwd, "rev-parse", "--show-toplevel")
    if not root:
        return None, None
    try:
        relative = os.path.relpath(os.path.realpath(os.path.dirname(file_path)),
                                   os.path.realpath(root))
    except ValueError:
        return None, None
    if relative == ".." or relative.startswith(".." + os.sep):
        return None, None  # Outside the repository (scratch files, /tmp, ...).
    relative = os.path.normpath(os.path.join(relative, os.path.basename(file_path)))
    kind = plan_kind(relative)
    if kind is None:
        return None, None
    if not e3_repository(payload):
        return None, None
    base = base or state_dir(env)
    if searched_this_turn(payload, env, base):
        return None, None  # Respects the one-search guard: this turn already searched.
    remote = git(cwd, "remote", "get-url", "origin") or ""
    topic = digest(f"{remote}\n{plan_topic(relative)}", 64)
    state_path = _plan_state_path(sid, base)
    state_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with open(state_path.with_suffix(".lock"), "a", encoding="utf-8") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)  # Parallel Writes must not both take the last slot.
        state = _read_plan_state(state_path)
        if topic in state["topics"] or state["fires"] >= PLAN_CUE_MAX_PER_SESSION:
            return None, None
        state["fires"] += 1
        state["topics"].append(topic)
        tmp = state_path.with_name(f"{state_path.name}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(state), encoding="utf-8")
        tmp.replace(state_path)
    arm = sesh_arm(payload, env)
    metadata = {"sesh_plan_cue": "withheld" if arm == "holdout" else "shown",
                "sesh_plan_kind": kind, "sesh_plan_topic_sha256": topic,
                "sesh_plan_cue_index": state["fires"]}
    if arm == "holdout":
        return None, metadata
    return PLAN_CUE.format(kind=kind, path=relative), metadata
