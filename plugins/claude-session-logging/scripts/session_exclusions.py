#!/usr/bin/env python3
"""Owner session exclusions for Claude Session Logging (COR-5002).

Reads the same two lists Codex Session Logging honours:

* ``$SHERLOCK_HOME/session-exclusions.json`` (default
  ``~/.sherlock/session-exclusions.json``), the Sherlock collector's policy::

      {"version": 1,
       "excluded_session_ids": ["<uuid>", ...],
       "excluded_project_paths": ["/absolute/project/path", ...],
       "allowed_claude_session_ids": ["<uuid>", ...]}

* ``$CODEX_HOME/session-logging/excluded_sessions.json`` (default
  ``~/.codex/session-logging/excluded_sessions.json``), the Codex logger's own
  ``{"excluded_session_ids": [...]}`` list.

The rules mirror the Sherlock collector for ``claude_code`` sources:

* an excluded session id excludes that session and every Claude subagent of it;
* a session whose working directory is an excluded project path or inside one
  (including a git worktree of a checkout inside one) is excluded, and so are
  the subagents of a root session started there;
* when ``allowed_claude_session_ids`` is present, only those Claude root
  sessions are captured; subagent transcripts are not.

No file means no policy. A file that exists but cannot be read or validated
fails closed: nothing is captured or uploaded until it is repaired. Only the
policy files, git pointer files and the first transcript record naming a
working directory are read; the policy never leaves the machine.
"""
from __future__ import annotations

import json
import os
import re
import sys
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

POLICY_NAME = "session-exclusions.json"
WORKTREE_ORIGINS_NAME = "worktree-origins.json"
MAX_POLICY_BYTES = 1024 * 1024
MAX_GIT_FILE_BYTES = 64 * 1024
CWD_SCAN_LINES = 200
MAX_CWD_SCAN_BYTES = 8 * 1024 * 1024

EXCLUDED = "excluded_by_session_policy"
EXCLUDED_PROJECT = "excluded_by_project_policy"
NOT_ALLOWED = "not_in_claude_session_allowlist"
UNREADABLE = "session_exclusion_policy_unreadable"
UNVERIFIED = "session_ancestry_unverified"
ORIGINS_UNREADABLE = "worktree_origins_unreadable"
# The owner asked for these never to leave the machine: queued records are dropped.
DROP_REASONS = frozenset({EXCLUDED, EXCLUDED_PROJECT, NOT_ALLOWED})

_FOLD_CASE = sys.platform == "darwin"  # APFS is case-insensitive by default.
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}")
_PLACEHOLDER_IDS = frozenset({"unknown", "unknown-session"})


def sherlock_policy_path() -> Path:
    configured = os.environ.get("SHERLOCK_HOME")
    base = Path(configured).expanduser() if configured else Path.home() / ".sherlock"
    return base / POLICY_NAME


def codex_exclusions_path() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
    return codex_home / "session-logging" / "excluded_sessions.json"


def _normalize(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip().lower()
    return value if value and value not in _PLACEHOLDER_IDS else None


def _id_list(raw: dict, key: str) -> frozenset[str] | None:
    if key not in raw:
        return None
    values = raw[key]
    if not isinstance(values, list) or not all(isinstance(value, str) for value in values):
        raise ValueError(f"{key} must be a list of strings")
    return frozenset(value for value in map(_normalize, values) if value)


def _path_key(path: str) -> str:
    if not _FOLD_CASE:
        return path
    return unicodedata.normalize("NFD", unicodedata.normalize("NFD", path).casefold())


def _project_keys(raw: dict) -> frozenset[str]:
    values = raw.get("excluded_project_paths", [])
    if not isinstance(values, list):
        raise ValueError("excluded_project_paths must be a list of strings")
    keys: set[str] = set()
    for value in values:
        if not isinstance(value, str) or not value.strip() or "\0" in value:
            raise ValueError("project paths must be non-empty strings")
        expanded = os.path.expanduser(value.strip())
        if not os.path.isabs(expanded):
            raise ValueError("project paths must be absolute")
        normalized = os.path.normpath(expanded)
        resolved = os.path.realpath(expanded)
        if os.sep in (normalized, resolved):
            raise ValueError("excluded_project_paths must not exclude the filesystem root")
        keys |= {_path_key(normalized), _path_key(resolved)}
    return frozenset(keys)


def _read_object(path: Path) -> dict | None:
    """The JSON object in path; None when there is no file; raises when unusable."""
    try:
        with path.open("rb") as handle:
            data = handle.read(MAX_POLICY_BYTES + 1)
    except FileNotFoundError:
        return None
    if len(data) > MAX_POLICY_BYTES:
        raise ValueError(f"{path.name} is too large")
    raw = json.loads(data)
    if not isinstance(raw, dict):
        raise ValueError(f"{path.name} must contain a JSON object")
    return raw


@dataclass(frozen=True)
class ExclusionPolicy:
    excluded: frozenset[str] = frozenset()
    allowed_claude: frozenset[str] | None = None
    excluded_projects: frozenset[str] = frozenset()
    worktree_origins_path: Path | None = None
    error: str | None = None
    _cache: dict = field(default_factory=dict, compare=False, repr=False)

    @property
    def active(self) -> bool:
        return bool(self.error or self.excluded or self.excluded_projects or self.allowed_claude is not None)

    def blocked_reason(
        self,
        session_id: object,
        *,
        parent_session_id: object = None,
        cwds: Iterable[object] = (),
        transcript_path: object = None,
    ) -> str | None:
        """Why this Claude session (or subagent) must not be captured or uploaded.

        ``session_id`` is the hook or record session id. ``parent_session_id`` is
        the root session when the source is a subagent; a transcript under
        ``<root>/subagents/`` supplies it on its own. ``cwds`` are working
        directories already known for the source (hook payload or record).
        """
        if not self.active:
            return None
        if self.error:
            return UNREADABLE
        native = _normalize(session_id)
        parent = _normalize(parent_session_id)
        path = Path(transcript_path).expanduser() if isinstance(transcript_path, str) and transcript_path else None
        if path is not None and parent is None:
            parent, agent = subagent_ids(path)
            if parent is not None and (native is None or native == parent):
                native = agent or native
        if {native, parent} & self.excluded:
            return EXCLUDED
        if native is None:
            return UNVERIFIED
        if self.excluded_projects:
            blocked = self._project_reason(parent, path, cwds)
            if blocked:
                return blocked
        if self.allowed_claude is not None and (parent is not None or native not in self.allowed_claude):
            return NOT_ALLOWED
        return None

    def _project_reason(self, parent: str | None, path: Path | None, cwds: Iterable[object]) -> str | None:
        own = {cwd for cwd in cwds if isinstance(cwd, str) and os.path.isabs(cwd)}
        if path is not None:
            cwd = self._first_cwd(path)
            if cwd:
                own.add(cwd)
        known = set(own)
        if parent is not None:
            # A subagent transcript's root sits beside its directory; a subagent
            # hook payload already names the root session's own transcript.
            root = None
            if path is not None:
                root = root_transcript_for(path) or (path if subagent_ids(path)[0] is None else None)
            root_cwd = self._first_cwd(root) if root is not None else None
            if root_cwd is None:
                if any(map(self.project_excluded, known)):
                    return EXCLUDED_PROJECT
                return UNVERIFIED
            known.add(root_cwd)
        if not known:
            return UNVERIFIED
        if any(map(self.project_excluded, known)):
            return EXCLUDED_PROJECT
        if self._worktree_origins() is None:
            return ORIGINS_UNREADABLE
        return None

    def project_excluded(self, cwd: str) -> bool:
        key = ("cwd", cwd)
        if key not in self._cache:
            self._cache[key] = any(
                candidate == project or candidate.startswith(project.rstrip(os.sep) + os.sep)
                for candidate in self._cwd_candidates(cwd)
                for project in self.excluded_projects
            )
        return self._cache[key]

    def _cwd_candidates(self, cwd: str) -> set[str]:
        """Every spelling of cwd plus the main checkout of any git worktree holding it."""
        normalized = os.path.normpath(cwd)
        resolved = os.path.realpath(cwd)
        origins = set(_git_origins(normalized).values()) | set(_git_origins(resolved).values())
        spellings = {_path_key(normalized), _path_key(resolved)}
        # Worktrees the Sherlock collector already saw keep classifying after removal.
        for root, recorded in (self._worktree_origins() or {}).items():
            prefix = _path_key(root).rstrip(os.sep)
            if any(value == prefix or value.startswith(prefix + os.sep) for value in spellings):
                origins.update(recorded)
        candidates = {normalized, resolved} | origins | {os.path.realpath(origin) for origin in origins}
        return {_path_key(candidate) for candidate in candidates}

    def _worktree_origins(self) -> dict[str, list[str]] | None:
        if "origins" not in self._cache:
            self._cache["origins"] = _read_worktree_origins(self.worktree_origins_path)
        return self._cache["origins"]

    def _first_cwd(self, path: Path) -> str | None:
        key = ("file-cwd", str(path))
        if key not in self._cache:
            self._cache[key] = first_transcript_cwd(path)
        return self._cache[key]


def load_policy() -> ExclusionPolicy:
    """Merge the Sherlock policy and the Codex logger's list; any unusable file fails closed."""
    sherlock = sherlock_policy_path()
    excluded: set[str] = set()
    allowed: frozenset[str] | None = None
    projects: frozenset[str] = frozenset()
    try:
        raw = _read_object(sherlock)
        if raw is not None:
            if raw.get("version") != 1:
                raise ValueError("policy must be an object with version 1")
            excluded |= _id_list(raw, "excluded_session_ids") or frozenset()
            allowed = _id_list(raw, "allowed_claude_session_ids")
            projects = _project_keys(raw)
        codex = _read_object(codex_exclusions_path())
        if codex is not None:
            excluded |= _id_list(codex, "excluded_session_ids") or frozenset()
    except (OSError, ValueError, UnicodeDecodeError) as error:
        return ExclusionPolicy(error=f"{type(error).__name__}: {error}")
    return ExclusionPolicy(
        excluded=frozenset(excluded),
        allowed_claude=allowed,
        excluded_projects=projects,
        worktree_origins_path=sherlock.parent / WORKTREE_ORIGINS_NAME,
    )


def subagent_ids(path: Path) -> tuple[str | None, str | None]:
    """(root session id, agent id) for ``<root>/subagents/**/agent-<id>.jsonl``, else (None, None)."""
    parts = [part.lower() for part in path.parts]
    if "subagents" not in parts:
        return None, None
    index = len(parts) - 1 - parts[::-1].index("subagents")
    root = parts[index - 1] if index > 0 else ""
    if not _UUID.fullmatch(root):
        return None, None
    name = parts[-1]
    agent = name[len("agent-"):-len(".jsonl")] if name.startswith("agent-") and name.endswith(".jsonl") else None
    return root, agent or None


def root_transcript_for(path: Path) -> Path | None:
    """``<slug>/<root>.jsonl`` for a subagent transcript under ``<slug>/<root>/subagents/``."""
    parts = path.parts
    lowered = [part.lower() for part in parts]
    if "subagents" not in lowered:
        return None
    index = len(lowered) - 1 - lowered[::-1].index("subagents")
    if index < 2:
        return None
    return Path(*parts[: index - 1]) / f"{parts[index - 1]}.jsonl"


def first_transcript_cwd(path: Path) -> str | None:
    """The first absolute ``cwd`` in a transcript's opening records, never the bodies after it."""
    try:
        with path.open("rb") as handle:
            budget = MAX_CWD_SCAN_BYTES
            for _ in range(CWD_SCAN_LINES):
                line = handle.readline(budget)
                if not line:
                    return None
                budget -= len(line)
                if budget <= 0:
                    return None
                if b'"cwd"' not in line:
                    continue
                try:
                    record = json.loads(line)
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                cwd = record.get("cwd") if isinstance(record, dict) else None
                if isinstance(cwd, str) and os.path.isabs(cwd):
                    return cwd
    except OSError:
        return None
    return None


def _read_worktree_origins(path: Path | None) -> dict[str, list[str]] | None:
    """Sherlock's remembered worktree roots (read only). None when present but unreadable."""
    if path is None:
        return {}
    try:
        data = path.read_bytes()
    except FileNotFoundError:
        return {}
    except OSError:
        return None
    try:
        raw = json.loads(data)
    except ValueError:
        return None
    origins = raw.get("origins") if isinstance(raw, dict) else None
    if isinstance(origins, dict):
        origins = {key: [values] if isinstance(values, str) else values for key, values in origins.items()}
    if not isinstance(origins, dict) or not all(
        isinstance(key, str)
        and isinstance(values, list)
        and all(isinstance(value, str) and os.path.isabs(value) for value in values)
        for key, values in origins.items()
    ):
        return None
    return origins


def _git_origins(cwd: str) -> dict[str, str]:
    """Map each linked git worktree root containing cwd to its main checkout (pointer files only)."""
    origins: dict[str, str] = {}
    current = cwd
    while True:
        marker = os.path.join(current, ".git")
        gitdir = _git_pointer(marker, "gitdir:") if os.path.isfile(marker) else None
        if gitdir is not None:
            gitdir = os.path.normpath(os.path.join(current, gitdir))
            common = _git_pointer(os.path.join(gitdir, "commondir"), "")
            common = os.path.normpath(os.path.join(gitdir, common)) if common else gitdir
            origins[current] = os.path.dirname(common) if os.path.basename(common) == ".git" else common
        parent = os.path.dirname(current)
        if parent == current:
            return origins
        current = parent


def _git_pointer(path: str, prefix: str) -> str | None:
    try:
        with open(path, "rb") as handle:
            text = handle.read(MAX_GIT_FILE_BYTES).decode("utf-8").strip()
    except (OSError, UnicodeDecodeError):
        return None
    if not text.startswith(prefix):
        return None
    return text[len(prefix):].strip() or None
