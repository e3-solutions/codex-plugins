"""Local-only Sesh boundary guidance; never performs a search or stores a query."""
import hashlib
import json
import os
import re
import stat
import subprocess
import uuid
from datetime import datetime, timezone

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

# COR-4688: the cue after compaction is off for everyone (bots included). It drove 29% of
# Codex Sesh searches with ~13% opened and ~2.7% yield (COR-4681). E3_SESH_CUE_AFTER_COMPACTION
# set to 1/true/on brings back the exact pre-0.2.35 cue at compaction.
AFTER_COMPACTION_ENV = "E3_SESH_CUE_AFTER_COMPACTION"
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

# COR-4681: startup-cue on/off experiment. Off unless E3_SESH_CUE_EXPERIMENT is 1/true/on.
EXPERIMENT_ID = "sesh_cue_v1"
EXPERIMENT_ENV = "E3_SESH_CUE_EXPERIMENT"
EXPERIMENT_SALT_ENV = "E3_SESH_CUE_EXPERIMENT_SALT"
EXPERIMENT_OPTOUT_ENV = "E3_SESH_EXPERIMENT_OPTOUT"
DISABLE_ENVS = ("E3_SESH_CONTEXT_ENABLED", "E3_SESH_START_SEARCH_ENABLED")
# cue/no_cue are the randomized arms; the rest always keep today's behaviour.
EXPERIMENT_ARMS = ("cue", "no_cue", "optout", "bot", "disabled", "unassigned")
WEEK_SALT_PATTERN = re.compile(r"[0-9]{4}-W(?:0[1-9]|[1-4][0-9]|5[0-3])")

# COR-4808: "fewer routine searches" A/B. On by default since Oct 16, 2026; E3_SESH_ROUTINE_AB=0/false/no/off
# turns it off and restores today's cue.
# Arm need_only gets no routine Sesh cue (the startup cue, and the compaction cue when
# it is turned back on); Sesh tools, the search-coding-sessions skill and all logging
# stay, so the agent still searches when it lacks something. The arm is a function of
# the session id alone, so analysis recomputes it from the caller session id:
#   arm = ROUTINE_ARMS[sha256("sesh_routine_v1:" + lower(trim(id)))[0] & 1]
#   SQL: get_byte(sha256(convert_to('sesh_routine_v1:' || lower(btrim(id)), 'UTF8')), 0) % 2
#        (0 = routine, 1 = need_only)
ROUTINE_AB_ID = "sesh_routine_v1"
ROUTINE_AB_ENV = "E3_SESH_ROUTINE_AB"
ROUTINE_ARMS = ("routine", "need_only")
# COR-4887: session-level holdout, identical to plugins/claude-sesh (COR-4888) so a session id lands
# in the same arm in both tools. E3_SESH_HOLDOUT_PCT (default 15, 0 turns it off, 0-100) of
# human sessions get no Sesh nudge at all: no startup cue, no plan cue, no past-work cue, and
# no one-search-per-turn guard. Sesh tools, the skill and all logging stay. Bots (automated
# threads), opt-outs and unreadable threads are never held out. The holdout is decided first;
# the routine A/B above then splits the treated sessions with its own independent hash:
#   bucket = (b0 * 256 + b1) % 100 where b = sha256("sesh_holdout_v1:" + lower(trim(id)))
#   SQL: (get_byte(h, 0) * 256 + get_byte(h, 1)) % 100 < 15  -> holdout
#        with h = sha256(convert_to('sesh_holdout_v1:' || lower(btrim(id)), 'UTF8'))
HOLDOUT_ID = "sesh_holdout_v1"
HOLDOUT_ENV = "E3_SESH_HOLDOUT_PCT"
HOLDOUT_DEFAULT_PCT = 15
# sesh_arm, logged on every environment snapshot and Sesh event: holdout/treated are the
# randomized arms; bot/optout/unassigned are never randomized and keep every nudge.
SESH_ARMS = ("holdout", "treated", "bot", "optout", "unassigned")
# Native Codex thread sources a person drives; anything else is an automated thread
# (agent_created_thread, guardian_review, subagent, ...). Same split as publish_presence.
HUMAN_THREAD_SOURCES = frozenset({"user"})
HEADER_MAX_BYTES = 524288
_UUID7_MIN_MS = 1577836800000  # 2020-01-01
_UUID7_MAX_MS = 4102444800000  # 2100-01-01


def _falsy(value):
    return value is not None and value.strip().lower() in {"0", "false", "no", "off"}


def opted_out(env):
    value = env.get(EXPERIMENT_OPTOUT_ENV)
    return value is not None and bool(value.strip()) and not _falsy(value)


def _truthy(env, name):
    return env.get(name, "").strip().lower() in {"1", "true", "on"}


def experiment_enabled(env=None):
    env = os.environ if env is None else env
    return _truthy(env, EXPERIMENT_ENV)


def cue_after_compaction(env=None):
    env = os.environ if env is None else env
    return _truthy(env, AFTER_COMPACTION_ENV)


def routine_ab_enabled(env=None):
    """On by default (Vansh turned the live A/B on, Oct 16, 2026); E3_SESH_ROUTINE_AB=0/false/no/off turns it off."""
    env = os.environ if env is None else env
    return not _falsy(env.get(ROUTINE_AB_ENV))


def routine_arm(session_id):
    """COR-4808 arm from the session id alone: "routine" (today's cue) or "need_only" (none)."""
    normalized = str(session_id).strip().lower()
    digest = hashlib.sha256(f"{ROUTINE_AB_ID}:{normalized}".encode("utf-8")).digest()
    return ROUTINE_ARMS[digest[0] & 1]


def holdout_pct(env=None):
    """E3_SESH_HOLDOUT_PCT as 0-100, exactly as claude-sesh reads it: unset or not a number means 15."""
    env = os.environ if env is None else env
    raw = env.get(HOLDOUT_ENV)
    if raw is None or not str(raw).strip():
        return HOLDOUT_DEFAULT_PCT
    try:
        value = int(str(raw).strip())
    except ValueError:
        return HOLDOUT_DEFAULT_PCT
    return min(max(value, 0), 100)


def holdout_bucket(session_id):
    """0-99 from the session id alone (independent of the routine A/B hash)."""
    normalized = str(session_id).strip().lower()
    digest = hashlib.sha256(f"{HOLDOUT_ID}:{normalized}".encode("utf-8")).digest()
    return (digest[0] * 256 + digest[1]) % 100


def sesh_arm(payload, env=None):
    """COR-4887 arm of this session: holdout, treated, bot, optout or unassigned."""
    env = os.environ if env is None else env
    if opted_out(env):
        return "optout"
    session_id = _session_id(payload) if isinstance(payload, dict) else None
    header = transcript_header(payload) if session_id is not None else None
    if header is not None and automated_thread(header):
        return "bot"  # Bots always keep Sesh and its nudges.
    if session_id is None or header is None:
        return "unassigned"
    return "holdout" if holdout_bucket(session_id) < holdout_pct(env) else "treated"


def held_out(payload, env=None):
    """True when this human session is in the no-nudge holdout. Any error means not held out."""
    try:
        return sesh_arm(payload, env) == "holdout"
    except Exception:  # noqa: BLE001 - fail open to today's behaviour.
        return False


def arm_metadata(payload, env=None):
    """Labels for the event log: sesh_arm, plus sesh_routine_arm for treated sessions while the A/B is on."""
    env = os.environ if env is None else env
    try:
        arm = sesh_arm(payload, env)
    except Exception:  # noqa: BLE001
        return {}
    metadata = {"sesh_arm": arm, "sesh_holdout_pct": holdout_pct(env)}
    if arm == "treated" and routine_ab_enabled(env):
        metadata["sesh_routine_arm"] = routine_arm(_session_id(payload))
    return metadata


def uuid7_datetime(session_id):
    """Creation time embedded in a UUIDv7 session id (first 48 bits are unix ms), else None."""
    try:
        parsed = uuid.UUID(str(session_id).strip())
    except (ValueError, AttributeError, TypeError):
        return None
    if parsed.version != 7:
        return None
    millis = parsed.int >> 80
    if not _UUID7_MIN_MS <= millis < _UUID7_MAX_MS:
        return None
    return datetime.fromtimestamp(millis / 1000, tz=timezone.utc)


def iso_week_salt(moment):
    year, week, _ = moment.isocalendar()
    return f"{year:04d}-W{week:02d}"


def week_salt(session_id, now=None, override=None):
    """ISO week of the session's creation, so compaction never re-randomizes a session."""
    if override is not None and WEEK_SALT_PATTERN.fullmatch(override.strip()):
        return override.strip()
    moment = uuid7_datetime(session_id) or now or datetime.now(timezone.utc)
    return iso_week_salt(moment)


def assign_arm(salt, session_id):
    """Pure 50/50 split on sha256(salt + ":" + session id)."""
    digest = hashlib.sha256(f"{salt}:{session_id}".encode("utf-8")).digest()
    return "cue" if digest[0] & 1 == 0 else "no_cue"


def _session_id(payload):
    for key in ("session_id", "sessionId"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip().lower()
    return None


def transcript_header(payload):
    """Bounded read of the rollout's session_meta header; never reads turns."""
    path = payload.get("transcript_path") or payload.get("transcriptPath")
    if not isinstance(path, str) or not os.path.isabs(path):
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as stream:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                return None
            raw = stream.readline(HEADER_MAX_BYTES + 1)
        if len(raw) > HEADER_MAX_BYTES:
            return None
        record = json.loads(raw)
    except (OSError, ValueError, UnicodeError, RecursionError):
        return None
    if not isinstance(record, dict) or record.get("type") != "session_meta":
        return None
    meta = record.get("payload")
    return meta if isinstance(meta, dict) else None


def automated_thread(header):
    """True for threads no person started: agent-created, guardian, subagent, codex exec."""
    thread_source = header.get("thread_source")
    if isinstance(thread_source, str) and thread_source and thread_source not in HUMAN_THREAD_SOURCES:
        return True
    source = header.get("source")
    if isinstance(source, dict):  # {"subagent": ...}
        return True
    if source == "exec" or header.get("originator") == "codex_exec":
        return True
    return False


def _boundary(payload):
    return (
        isinstance(payload, dict)
        and payload.get("hook_event_name", "SessionStart") == "SessionStart"
        and payload.get("source") in {"startup", "compact"}
    )


def _e3_repository(payload):
    cwd = payload.get("cwd")
    if not isinstance(cwd, str) or not cwd:
        return False
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


def _experiment_metadata(arm, salt):
    return {"sesh_cue_experiment": EXPERIMENT_ID, "sesh_cue_arm": arm, "sesh_cue_salt": salt}


def sesh_cue_decision(payload, env=None, now=None):
    """Return (cue text or None, experiment metadata or None).

    The cue is shown at startup only. After compaction it is withheld unless
    E3_SESH_CUE_AFTER_COMPACTION is on; the experiment arm is still logged there.
    """
    cue, metadata, _ = startup_nudge(payload, env, now)
    return cue, metadata


def startup_nudge(payload, env=None, now=None):
    """(cue or None, experiment metadata or None, COR-4887 exposure labels or None).

    The labels say whether today's routine cue was shown or withheld (holdout or need_only), so
    exposure is logged separately from the session's assigned arm.
    """
    env = os.environ if env is None else env
    compact = isinstance(payload, dict) and payload.get("source") == "compact"
    if compact and not cue_after_compaction(env) and not experiment_enabled(env):
        return None, None, None  # Nothing to show or log; skip the git lookup.
    cue, metadata = _boundary_cue_decision(payload, env, now)
    if cue is not None and compact:
        cue = COMPACTION_CONTEXT if cue_after_compaction(env) else None
    if cue is None:
        return None, metadata, None
    if held_out(payload, env):
        # COR-4887 holdout: no Sesh nudge of any kind; Sesh itself is untouched.
        return None, metadata, {"sesh_startup_cue": "withheld", "sesh_withheld_by": "holdout"}
    if routine_cue_withheld(payload, env):
        # COR-4808 need_only arm: no routine cue; Sesh itself is untouched.
        return None, metadata, {"sesh_startup_cue": "withheld", "sesh_withheld_by": "need_only"}
    return cue, metadata, {"sesh_startup_cue": "shown"}


def routine_cue_withheld(payload, env=None):
    """True only while E3_SESH_ROUTINE_AB is on and this human session is in arm need_only.

    Opt-outs, automated threads (bots keep Sesh) and unreadable threads or ids keep
    today's cue, the same rules as COR-4681. Nothing is read when the flag is off.
    """
    env = os.environ if env is None else env
    if not routine_ab_enabled(env) or opted_out(env) or not isinstance(payload, dict):
        return False
    session_id = _session_id(payload)
    if session_id is None:
        return False
    header = transcript_header(payload)
    if header is None or automated_thread(header):
        return False
    return routine_arm(session_id) == "need_only"


def _boundary_cue_decision(payload, env, now):
    enabled = experiment_enabled(env)
    disabled = any(_falsy(env.get(name)) for name in DISABLE_ENVS)
    if disabled and not enabled:
        return None, None
    if not _boundary(payload):
        return None, None
    session_id = _session_id(payload)
    salt = week_salt(session_id, now=now, override=env.get(EXPERIMENT_SALT_ENV)) if enabled else None
    if disabled:  # The existing kill switch always wins: no cue, nothing randomized.
        return None, _experiment_metadata("disabled", salt)
    if not _e3_repository(payload):
        return None, None
    if not enabled:
        return CONTEXT, None
    if opted_out(env):
        return CONTEXT, _experiment_metadata("optout", salt)
    header = transcript_header(payload)
    if header is not None and automated_thread(header):
        return CONTEXT, _experiment_metadata("bot", salt)  # Bots always keep Sesh.
    if session_id is None or header is None:
        return CONTEXT, _experiment_metadata("unassigned", salt)
    arm = assign_arm(salt, session_id)
    return (CONTEXT if arm == "cue" else None), _experiment_metadata(arm, salt)


def sesh_context(payload):
    context, _ = sesh_cue_decision(payload)
    return context
