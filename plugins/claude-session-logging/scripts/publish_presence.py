#!/usr/bin/env python3
"""Publish metadata-only presence for open Claude Code sessions.

Claude Code has no native SQLite state database like Codex. The equivalent
"session is open" signal is the transcript file at
``~/.claude/projects/<slug>/<sessionId>.jsonl`` — its name is the session id and
each turn appends a line, so its mtime tracks activity. This module reads those
files (scoped to e3-solutions repositories), and republishes a deterministic
``resident_presence`` event per recently-active session through the plugin's
existing ingest queue so an idle-but-open session stays live on the heartbeat
dashboard. It never reads prompts, responses, tool calls, or transcript bodies —
only the session id, cwd, repo/branch, and activity timestamp.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import session_logging

JsonDict = dict[str, Any]

CLAUDE_PROJECTS_DIR_ENV = "CLAUDE_SESSION_LOG_PROJECTS_DIR"
PRESENCE_STATE_ENV = "CLAUDE_SESSION_LOG_PRESENCE_STATE"
# Only republish presence for sessions touched within this window; this is the
# same 5 minute freshness bound Codex uses for its resident presence.
MAX_PRESENCE_AGE_SECONDS = 5 * 60
DEFAULT_LOOKBACK_SECONDS = 24 * 60 * 60
DEFAULT_LIMIT = 500
CONTEXT_SCAN_LINES = 200


def default_projects_dir() -> Path:
    override = os.environ.get(CLAUDE_PROJECTS_DIR_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return (Path.home() / ".claude" / "projects").resolve()


def default_state_path() -> Path:
    override = os.environ.get(PRESENCE_STATE_ENV)
    if override:
        return Path(override).expanduser().resolve()
    return session_logging.state_dir() / "presence" / "state.json"


def iter_session_files(projects_dir: Path) -> list[Path]:
    if not projects_dir.exists():
        return []
    return sorted(projects_dir.glob("*/*.jsonl"))


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat()


def read_session_context(path: Path) -> JsonDict | None:
    """Return {cwd, git_branch} read from the first transcript lines, or None."""
    try:
        with path.open(encoding="utf-8") as handle:
            for index, line in enumerate(handle):
                if index >= CONTEXT_SCAN_LINES:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    loaded = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(loaded, dict):
                    continue
                cwd = loaded.get("cwd")
                if isinstance(cwd, str) and cwd:
                    branch = loaded.get("gitBranch")
                    return {
                        "cwd": cwd,
                        "git_branch": branch if isinstance(branch, str) and branch else None,
                    }
    except OSError:
        return None
    return None


def session_target(path: Path) -> JsonDict | None:
    """Resolve a transcript file to an e3-solutions presence target, or None."""
    context = read_session_context(path)
    if context is None:
        return None
    cwd = str(context["cwd"])
    remote = session_logging.git_origin_remote(cwd)
    if not session_logging.remote_belongs_to_org(remote, session_logging.allowed_github_org()):
        return None
    return {
        "session_id": path.stem,
        "cwd": cwd,
        "git_branch": context.get("git_branch"),
        "repo_remote": remote,
        "transcript_path": str(path.resolve()),
    }


def presence_record(
    target: JsonDict,
    *,
    observed_at: str,
    base: Path,
    ended: bool = False,
) -> JsonDict:
    session_id = session_logging.safe_segment(str(target["session_id"]))
    transcript_path = str(target["transcript_path"])
    event_id = session_logging.sha256_hex(f"claude-presence:{session_id}")[:32]
    event_type = "resident_presence"
    storage_path = f"users/local/sessions/{session_id}/events/000000-{event_type}.json"
    metadata: JsonDict = {
        "platform": session_logging.PLATFORM,
        "agent": session_logging.AGENT,
        "cwd": str(target["cwd"]),
        "transcript_path": transcript_path,
        "source": "resident_presence",
    }
    branch = target.get("git_branch")
    if isinstance(branch, str) and branch:
        metadata["git_branch"] = branch
    detail: JsonDict = {
        "id": event_id,
        "session_id": session_id,
        "seq": 0,
        "event_type": event_type,
        "hook_event_name": "ResidentPresence",
        "created_at": observed_at,
        "metadata": metadata,
        "thread_id": session_logging.sha256_hex(transcript_path),
    }
    session_logging.write_json_atomic(base / storage_path, detail)
    record: JsonDict = {
        **detail,
        "type": "event",
        "storage_bucket": session_logging.bucket_name(),
        "storage_path": storage_path,
        "local_content_path": storage_path,
        "uploaded_at": None,
    }
    if ended:
        record["ended_at"] = observed_at
    return record


def publish_target(target: JsonDict, *, observed_at: str, ended: bool = False) -> JsonDict:
    base = session_logging.ensure_state_dir()
    record = presence_record(target, observed_at=observed_at, base=base, ended=ended)
    session_logging.enqueue_record(base, record)
    return record


def load_state(path: Path) -> JsonDict:
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {}
    return loaded if isinstance(loaded, dict) else {}


def write_state(path: Path, state: JsonDict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def publish_one(
    session_id: str,
    transcript_path: str | Path,
    *,
    now: datetime | None = None,
    ended: bool = False,
) -> JsonDict:
    """Publish (and drain) a single session's presence — used by the ticker."""
    path = Path(transcript_path).expanduser()
    if not path.exists():
        return {"published": 0, "reason": "missing_transcript"}
    if not session_logging.upload_configured():
        return {"published": 0, "reason": "disabled"}
    target = session_target(path)
    if target is None:
        return {"published": 0, "reason": "not_eligible"}
    target["session_id"] = session_id or target["session_id"]
    observed_at = (now or now_utc()).isoformat() if ended else mtime_iso(path)
    publish_target(target, observed_at=observed_at, ended=ended)
    drain = session_logging.drain_queue()
    return {"published": 1, "ended": ended, "drain": drain}


def run_presence(
    *,
    projects_dir: str | Path | None = None,
    state_path: str | Path | None = None,
    now: datetime | None = None,
    lookback_seconds: int = DEFAULT_LOOKBACK_SECONDS,
    limit: int = DEFAULT_LIMIT,
) -> JsonDict:
    root = Path(projects_dir or default_projects_dir()).expanduser().resolve()
    state_file = Path(state_path or default_state_path()).expanduser().resolve()
    state_file.parent.mkdir(parents=True, exist_ok=True)
    lock_path = state_file.with_suffix(state_file.suffix + ".lock")
    with lock_path.open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"locked": True, "published": 0}

        current = now or now_utc()
        if not session_logging.upload_configured():
            return {"disabled": True, "published": 0}

        state = load_state(state_file)
        published_state = state.get("published")
        if not isinstance(published_state, dict):
            published_state = {}

        cutoff = current.timestamp() - max(1, int(lookback_seconds))
        fresh_cutoff = current.timestamp() - MAX_PRESENCE_AGE_SECONDS
        published = 0
        scanned = 0
        for path in iter_session_files(root):
            if published >= max(1, int(limit)):
                break
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime < cutoff or mtime < fresh_cutoff:
                # Not active within the presence freshness window; leave it to
                # go stale on the dashboard rather than re-asserting liveness.
                continue
            target = session_target(path)
            if target is None:
                continue
            scanned += 1
            session_id = session_logging.safe_segment(str(target["session_id"]))
            previous = published_state.get(session_id)
            if isinstance(previous, (int, float)) and mtime <= float(previous):
                continue
            publish_target(target, observed_at=mtime_iso(path))
            published_state[session_id] = mtime
            published += 1

        # Prune state entries outside the lookback window.
        published_state = {
            key: value
            for key, value in published_state.items()
            if isinstance(value, (int, float)) and float(value) >= cutoff
        }
        drain = session_logging.drain_queue() if published else {"uploaded": 0}
        state.update(
            {
                "schema": 1,
                "last_checked_at": current.isoformat(),
                "published": published_state,
            }
        )
        write_state(state_file, state)
        return {"scanned": scanned, "published": published, "drain": drain}


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Publish presence for open Claude Code sessions.")
    parser.add_argument("--projects-dir")
    parser.add_argument("--state-path")
    parser.add_argument("--lookback-seconds", type=int, default=DEFAULT_LOOKBACK_SECONDS)
    parser.add_argument("--limit", type=int, default=DEFAULT_LIMIT)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run_presence(
            projects_dir=args.projects_dir,
            state_path=args.state_path,
            lookback_seconds=args.lookback_seconds,
            limit=args.limit,
        )
    except Exception as exc:  # noqa: BLE001 - presence must never disrupt Claude Code.
        result = {"error": str(exc), "published": 0}
    if not args.quiet or result.get("error") or result.get("published"):
        print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
