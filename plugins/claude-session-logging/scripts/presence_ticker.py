#!/usr/bin/env python3
"""Detached heartbeat ticker for a single open Claude Code session.

Spawned (detached, own session group) by ``session_start.py``. Every ~60s it
republishes metadata-only presence for its session while the transcript file is
fresh, so an idle-but-open Claude session keeps its heartbeat on the dashboard
between turns. After ~5 minutes with no transcript activity it publishes one
final ``ended`` presence (setting codex_sessions.ended_at) and exits. A per
session flock keeps a new session from stacking a second ticker.
"""
from __future__ import annotations

import argparse
import fcntl
import json
import os
import sys
import time
from datetime import timezone
from pathlib import Path

import publish_presence
import session_logging

DEFAULT_INTERVAL_SECONDS = 60
DEFAULT_IDLE_TIMEOUT_SECONDS = 5 * 60
# Absolute ceiling so a ticker can never outlive its usefulness if something
# keeps the transcript's mtime warm indefinitely.
MAX_LIFETIME_SECONDS = 12 * 60 * 60


def spawn(session_id: str, transcript_path: str) -> None:
    """Launch a detached ticker for a session. Best-effort; never raises."""
    if not session_id or not transcript_path:
        return
    if not session_logging.upload_configured():
        return
    if not presence_enabled():
        return
    script = Path(__file__).resolve()
    try:
        import subprocess

        subprocess.Popen(
            [
                sys.executable,
                str(script),
                "--session-id",
                session_id,
                "--transcript-path",
                transcript_path,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
            start_new_session=True,
        )
    except Exception:  # noqa: BLE001 - presence must never disrupt Claude Code.
        pass


def presence_enabled() -> bool:
    value = os.environ.get("CLAUDE_SESSION_LOG_PRESENCE", "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def lock_path_for(session_id: str) -> Path:
    base = session_logging.ensure_state_dir()
    safe = session_logging.safe_segment(session_id)
    directory = base / "presence" / "tickers"
    directory.mkdir(parents=True, exist_ok=True)
    return directory / f"{safe}.lock"


def transcript_mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def run(
    session_id: str,
    transcript_path: str,
    *,
    interval: float = DEFAULT_INTERVAL_SECONDS,
    idle_timeout: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
    max_lifetime: float = MAX_LIFETIME_SECONDS,
) -> dict:
    path = Path(transcript_path).expanduser()
    lock_path = lock_path_for(session_id)
    with lock_path.open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"ticker": "already_running"}

        started = time.monotonic()
        ticks = 0
        while True:
            if not presence_enabled() or not session_logging.upload_configured():
                return {"ticker": "disabled", "ticks": ticks}
            mtime = transcript_mtime(path)
            if mtime is None:
                _publish_end(session_id, transcript_path)
                return {"ticker": "transcript_gone", "ticks": ticks}
            idle = time.time() - mtime
            if idle >= idle_timeout or (time.monotonic() - started) >= max_lifetime:
                _publish_end(session_id, transcript_path)
                return {"ticker": "idle_ended", "ticks": ticks, "idle_seconds": idle}
            try:
                publish_presence.publish_one(session_id, transcript_path)
                ticks += 1
            except Exception:  # noqa: BLE001 - presence must never disrupt Claude Code.
                pass
            time.sleep(max(1.0, float(interval)))


def _publish_end(session_id: str, transcript_path: str) -> None:
    try:
        publish_presence.publish_one(
            session_id,
            transcript_path,
            now=publish_presence.now_utc().astimezone(timezone.utc),
            ended=True,
        )
    except Exception:  # noqa: BLE001 - presence must never disrupt Claude Code.
        pass


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Heartbeat a single open Claude Code session.")
    parser.add_argument("--session-id", required=True)
    parser.add_argument("--transcript-path", required=True)
    parser.add_argument("--interval", type=float, default=DEFAULT_INTERVAL_SECONDS)
    parser.add_argument("--idle-timeout", type=float, default=DEFAULT_IDLE_TIMEOUT_SECONDS)
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)
    try:
        result = run(
            args.session_id,
            args.transcript_path,
            interval=args.interval,
            idle_timeout=args.idle_timeout,
        )
    except Exception as exc:  # noqa: BLE001 - presence must never disrupt Claude Code.
        result = {"error": str(exc)}
    if not args.quiet:
        print(json.dumps(result, sort_keys=True))


if __name__ == "__main__":
    main()
