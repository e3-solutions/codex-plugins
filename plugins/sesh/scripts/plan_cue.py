"""COR-4887: "existing work" check at the planning moment. On unless E3_SESH_PLAN_CUE is 0/false/no/off.

Why: in the Oct 19, 2026 missed-reuse study about 1 in 100 human messages pointed the agent at
work a teammate had already built or decided, and 39% of those were trigger failures: Sesh was
never searched for that topic at that moment. Searches happen at session start; the need comes
later, when the agent is about to build something.

This is the Codex twin of the Claude Code plugin's plan cue (plugins/claude-sesh/scripts/sesh_cc.py,
COR-4888). The rules, constants, cue text, topic hash and log labels are the same, and
tests/test_sesh_cross_plugin.py pins that:

- It fires before apply_patch adds a new code file (Claude Code: before Write creates one) in a
  person's session in an e3-solutions repository. Tests, fixtures, generated, vendored, scratch and
  review/artifact folders never count (PLAN_SKIP_PATH, from the Oct 20 precision replay).
- At most twice a session and once per area (the file's first two folders in its repository,
  keyed by the repository's origin), never in a turn that already ran a Sesh search.
- A file added in a folder this session already changed is a continuation, not a new thing, and
  never cues (Codex sees every patch, so it can tell; from the same replay).
- A COR-4887 holdout session gets no cue; the moment is logged as withheld.

It only adds model-visible context; it never blocks. Each moment is logged as a content-free
`sesh_plan_cue_shown` / `sesh_plan_cue_withheld` event (kind, a topic hash, the fire index and the
session's arm). No path, prompt or query is logged or uploaded; local state holds hashes only.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
import posixpath
import re
import subprocess
import time
from pathlib import Path

from sesh_context import DISABLE_ENVS, _falsy, automated_thread, held_out, opted_out, transcript_header

PLAN_CUE_ENV = "E3_SESH_PLAN_CUE"
PLAN_CUE_MAX_PER_SESSION = 2
MAX_INPUT_CHARS = 4_000_000
STATE_MAX_AGE_SECONDS = 7 * 86400

# --- identical to plugins/claude-sesh/scripts/sesh_cc.py ------------------------------------------
PLAN_CUE = (
    "Sesh existing-work check: you are about to create a new {kind} ({path}). Before you "
    "build it, make one Sesh coding-session search for existing or teammate work that already "
    "does this (name the system and what it does; no file paths). If a result fits, show it as "
    "an existing-work card: link, owner, date, and one line on what that work did. Then reuse "
    "or extend it, or say in one line why not. If nothing fits, say so in one line and continue."
)
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
# were where the false cues landed (precision 47% -> see the PR). Shared with claude-sesh.
PLAN_SKIP_PATH = re.compile(
    r"(^|/)(outputs?|artifacts?|reviews?|visuali[sz]ations?|viz|analysis|analyses|investigations?|"
    r"reports?|docs?|examples?|notebooks?|evals?|benchmarks?|bench|playground|sandbox|archive|"
    r"backups?|data|logs?|debug|repro|qa|smoke|harness|seeds?|cor-\d+|[a-z]+-\d{2,}|"
    r"\d{4}-\d{2}-\d{2}[^/]*)/",
    re.IGNORECASE,
)
# --------------------------------------------------------------------------------------------------

_ADD_FILE = re.compile(r"^\*\*\* Add File: (.+?)\s*$", re.MULTILINE)
_ANY_FILE = re.compile(r"^\*\*\* (?:Add|Update|Delete) File: (.+?)\s*$", re.MULTILINE)
_SCRATCH = re.compile(r"^/(?:private/)?(?:tmp|var/folders)/")


def switch_on(env) -> bool:
    return not _falsy(env.get(PLAN_CUE_ENV))


def state_dir() -> Path:
    return Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sesh-plan-cue"


def digest(value: str, length: int = 32) -> str:
    """Same as sesh_cc.digest: sha256 of the trimmed, lower-cased value."""
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()[:length]


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


def git(cwd: str, *args: str) -> str | None:
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


_E3_REMOTE = re.compile(
    r"(?:https://github\.com/|git@github\.com:|ssh://git@github\.com/)e3-solutions/[A-Za-z0-9_.-]+",
    re.IGNORECASE,
)


def _repo(folder: str, cache: dict):
    """(repository root, origin) of an existing folder, cached per call; (None, None) when not a repo."""
    if folder not in cache:
        root = git(folder, "rev-parse", "--show-toplevel")
        cache[folder] = (root, git(root, "remote", "get-url", "origin") or "") if root else (None, None)
    return cache[folder]


def _existing_parent(path: str) -> str | None:
    folder = posixpath.dirname(path)
    for _ in range(32):
        if not folder or folder == "/":
            return None
        if os.path.isdir(folder):
            return folder
        folder = posixpath.dirname(folder)
    return None


def patch_paths(payload, pattern=_ANY_FILE) -> list[str]:
    tool_input = payload.get("tool_input")
    command = tool_input.get("command") if isinstance(tool_input, dict) else tool_input
    if payload.get("tool_name") != "apply_patch" or not isinstance(command, str) or len(command) > MAX_INPUT_CHARS:
        return []
    return [path.strip().strip("\"'") for path in pattern.findall(command)]


def resolve(path: str, cwd: str | None, cache: dict):
    """(repository root, origin, repo-relative path) for a patch path, else None (outside a repo, scratch)."""
    absolute = path if path.startswith("/") else (posixpath.join(cwd, path) if cwd and cwd.startswith("/") else None)
    if absolute is None:
        return None
    absolute = posixpath.normpath(absolute)
    if _SCRATCH.match(absolute):
        return None
    parent = _existing_parent(absolute)
    if parent is None:
        return None
    root, origin = _repo(parent, cache)
    if not root:
        return None
    root = posixpath.normpath(root)
    if not absolute.startswith(root.rstrip("/") + "/"):
        return None
    return root, origin, absolute[len(root.rstrip("/")) + 1:]


def candidates(payload) -> list[str]:
    """Cheap first pass, no git or state: the Add File paths of an apply_patch with a code-file name."""
    if not isinstance(payload, dict):
        return []
    found = []
    for path in patch_paths(payload, _ADD_FILE):
        name = posixpath.basename(path)
        if name == "Dockerfile" or (os.path.splitext(name)[1].lower() in CODE_SUFFIXES and name not in TRIVIAL_NAMES):
            found.append(path)
    return found


class _Locked:
    """Exclusive lock on one session's state file (a small JSON of hashes), updated in place."""

    def __init__(self, path: Path):
        self.path = path

    def __enter__(self):
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        fcntl.flock(self.fd, fcntl.LOCK_EX)
        raw = os.read(self.fd, 1 << 20)
        try:
            state = json.loads(raw) if raw else {}
        except ValueError:
            state = {}
        self.state = state if isinstance(state, dict) else {}
        return self.state

    def save(self):
        data = json.dumps(self.state, separators=(",", ":")).encode("utf-8")
        os.lseek(self.fd, 0, os.SEEK_SET)
        os.ftruncate(self.fd, 0)
        os.write(self.fd, data)

    def __exit__(self, *exc):
        fcntl.flock(self.fd, fcntl.LOCK_UN)
        os.close(self.fd)
        return False


def searched_this_turn(payload) -> bool:
    """True when the COR-4811 guard has a marker for this turn: Sesh was already searched in it."""
    session = payload.get("session_id") or payload.get("sessionId")
    turn = payload.get("turn_id") or payload.get("turnId")
    if not isinstance(session, str) or not isinstance(turn, str) or not session.strip() or not turn.strip():
        return False
    guard = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex") / "sesh-turn-guard"
    return (guard / digest(session) / digest(turn)).exists()


def _prune(folder: Path, now: float) -> None:
    stamp = folder / ".pruned"
    try:
        if now - stamp.stat().st_mtime < 3600:
            return
    except OSError:
        pass
    try:
        stamp.touch()
        for item in folder.iterdir():
            if item.name != ".pruned" and now - item.stat().st_mtime > STATE_MAX_AGE_SECONDS:
                item.unlink(missing_ok=True)
    except OSError:
        pass


def decide(payload, env=None, directory: Path | None = None, now: float | None = None):
    """(cue text or None, log metadata or None) for this PreToolUse call.

    Every apply_patch updates the session's set of changed folders (hashes); a cue needs a new
    code file in a folder the session has not changed yet.
    """
    env = os.environ if env is None else env
    if not switch_on(env) or not isinstance(payload, dict):
        return None, None
    if payload.get("hook_event_name", "PreToolUse") != "PreToolUse" or payload.get("tool_name") != "apply_patch":
        return None, None
    paths = patch_paths(payload)
    if not paths:
        return None, None
    session = payload.get("session_id") or payload.get("sessionId")
    if not isinstance(session, str) or not session.strip():
        return None, None
    cwd = payload.get("cwd") if isinstance(payload.get("cwd"), str) else None
    adds = set(patch_paths(payload, _ADD_FILE))
    cache: dict = {}
    eligible = (candidates(payload) and not opted_out(env) and not any(_falsy(env.get(n)) for n in DISABLE_ENVS))
    header = transcript_header(payload) if eligible else None
    eligible = eligible and header is not None and not automated_thread(header)  # People only, as in Claude Code.
    directory = directory or state_dir()
    now = time.time() if now is None else now
    result = (None, None)
    session_file = directory / "sessions" / digest(session)
    fresh = not session_file.exists()
    locked = _Locked(session_file)
    with locked as state:
        touched = set(state.get("touched", []))
        fires = state.get("fires") if isinstance(state.get("fires"), int) else 0
        topics = [t for t in state.get("topics", []) if isinstance(t, str)]
        changed_now = set()
        for path in paths:
            absolute = path if path.startswith("/") else (posixpath.join(cwd, path) if cwd else None)
            if absolute is None:
                continue
            folder = digest(posixpath.dirname(posixpath.normpath(absolute)), 16)
            changed_now.add(folder)
            if not eligible or path not in adds or result[1] is not None:
                continue
            if folder in touched:
                continue  # A continuation of work already under way in this folder.
            resolved = resolve(path, cwd, cache)
            if resolved is None:
                continue
            _root, origin, relative = resolved
            kind = plan_kind(relative)
            if kind is None or not _E3_REMOTE.fullmatch(origin or ""):
                continue
            if fires >= PLAN_CUE_MAX_PER_SESSION:
                continue
            topic = digest(f"{origin}\n{plan_topic(relative)}", 64)
            if topic in topics:
                continue
            if searched_this_turn(payload):
                continue  # Respects the one-search guard: this turn already searched.
            fires += 1
            topics.append(topic)
            holdout = held_out(payload, env)
            metadata = {"sesh_plan_cue": "withheld" if holdout else "shown",
                        "sesh_plan_kind": kind, "sesh_plan_topic_sha256": topic,
                        "sesh_plan_cue_index": fires}
            result = (None if holdout else PLAN_CUE.format(kind=kind, path=relative), metadata)
        if changed_now - touched or result[1] is not None:
            state.clear()
            state.update({"fires": fires, "topics": topics[-20:],
                          "touched": sorted(touched | changed_now)[-400:], "t": now})
            locked.save()
    if fresh:
        _prune(directory / "sessions", now)
    return result


def plan_cue(payload, env=None):
    """Fail-open wrapper: (hook JSON or None, log metadata or None). Never blocks the tool call."""
    try:
        cue, metadata = decide(payload, env)
    except Exception:  # noqa: BLE001 - the cue must never break or delay a tool call.
        return None, None
    if cue is None:
        return None, metadata
    return json.dumps({"hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": cue}}), metadata
