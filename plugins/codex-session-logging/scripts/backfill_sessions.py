#!/usr/bin/env python3
from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from session_logging import (
    ALLOWED_GITHUB_ORG,
    EXCERPT_BYTES,
    IngestUploader,
    PLUGIN_VERSION,
    append_jsonl,
    auto_upload_enabled,
    build_ingest_payload,
    bucket_name,
    client_context,
    drain_queue,
    enqueue_record,
    ensure_state_dir,
    git_origin_remote,
    now_iso,
    remote_belongs_to_org,
    safe_segment,
    sha256_hex,
    write_json_atomic,
)

JsonDict = dict[str, Any]
BACKFILL_VERSION = 1
BACKFILL_ENABLED_ENV = "CODEX_SESSION_LOG_BACKFILL"
BACKFILL_MAX_FILES_ENV = "CODEX_SESSION_LOG_BACKFILL_MAX_FILES"
DEFAULT_MAX_FILES = 1000


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Backfill historical Codex transcripts.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--max-files", type=int, default=max_files_per_run())
    args = parser.parse_args(argv)
    print(json.dumps(run_backfill(dry_run=args.dry_run, force=args.force, max_files=args.max_files), sort_keys=True))


def backfill_enabled() -> bool:
    value = os.environ.get(BACKFILL_ENABLED_ENV, "1").strip().lower()
    return value not in {"0", "false", "no", "off"}


def max_files_per_run() -> int:
    try:
        return max(1, int(os.environ.get(BACKFILL_MAX_FILES_ENV, DEFAULT_MAX_FILES)))
    except ValueError:
        return DEFAULT_MAX_FILES


def codex_home() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser().resolve() if configured else Path.home() / ".codex"


def transcript_paths() -> list[Path]:
    root = codex_home() / "sessions"
    return sorted(root.glob("**/*.jsonl")) if root.exists() else []


def backfill_dir(base: Path) -> Path:
    return base / "backfills" / f"v{BACKFILL_VERSION}"


def state_path(base: Path) -> Path:
    return backfill_dir(base) / "state.json"


def read_state(base: Path) -> JsonDict:
    try:
        loaded = json.loads(state_path(base).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return {"version": BACKFILL_VERSION, "files": {}}
    return loaded if isinstance(loaded, dict) else {"version": BACKFILL_VERSION, "files": {}}


def file_fingerprint(path: Path) -> str:
    stat = path.stat()
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def run_backfill(*, dry_run: bool = False, force: bool = False, max_files: int = DEFAULT_MAX_FILES) -> JsonDict:
    if not backfill_enabled():
        return {"version": BACKFILL_VERSION, "status": "disabled"}
    base = ensure_state_dir()
    directory = backfill_dir(base)
    directory.mkdir(parents=True, exist_ok=True)
    lock_path = directory / "worker.lock"
    with lock_path.open("a", encoding="utf-8") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"version": BACKFILL_VERSION, "status": "already_running"}

        state = read_state(base)
        files = state.setdefault("files", {})
        totals = {"discovered": 0, "processed": 0, "queued": 0, "skipped_non_e3": 0, "failed": 0}
        paths = transcript_paths()
        repo_by_cwd = known_repositories(paths)
        totals["discovered"] = len(paths)
        state.update({"version": BACKFILL_VERSION, "status": "running", "started_at": state.get("started_at") or now_iso()})
        if not dry_run:
            write_json_atomic(state_path(base), state)

        reporting_remote = string_value(state.get("reporting_remote"))
        for path in paths:
            if totals["processed"] >= max(1, max_files):
                break
            key = str(path)
            try:
                fingerprint = file_fingerprint(path)
            except OSError:
                totals["failed"] += 1
                continue
            previous = files.get(key) if isinstance(files.get(key), dict) else {}
            if (
                not force
                and previous.get("fingerprint") == fingerprint
                and previous.get("status") in {"complete", "skipped_non_e3"}
            ):
                continue
            try:
                result = import_transcript(path, base=base, dry_run=dry_run, repo_by_cwd=repo_by_cwd)
            except Exception as exc:  # noqa: BLE001 - one corrupt transcript must not stop the migration.
                totals["failed"] += 1
                files[key] = {"fingerprint": fingerprint, "status": "failed", "error": str(exc), "updated_at": now_iso()}
                if not dry_run:
                    append_jsonl(
                        directory / "failures.jsonl",
                        {"path": key, "error": str(exc), "created_at": now_iso()},
                    )
            else:
                totals["processed"] += 1
                totals["queued"] += int(result.get("queued", 0))
                if result.get("status") == "skipped_non_e3":
                    totals["skipped_non_e3"] += 1
                remote = result.get("repo_remote")
                if isinstance(remote, str):
                    reporting_remote = remote
                    state["reporting_remote"] = remote
                files[key] = {"fingerprint": fingerprint, **result, "updated_at": now_iso()}
            if not dry_run:
                state["totals"] = totals
                state["updated_at"] = now_iso()
                write_json_atomic(state_path(base), state)

        remaining = sum(
            1
            for path in paths
            if not isinstance(files.get(str(path)), dict)
            or files[str(path)].get("fingerprint") != file_fingerprint(path)
            or files[str(path)].get("status") not in {"complete", "skipped_non_e3"}
        )
        aggregate_totals = {
            "discovered": len(paths),
            "processed": sum(
                1 for value in files.values() if isinstance(value, dict) and value.get("status") in {"complete", "skipped_non_e3"}
            ),
            "queued": sum(
                int(value.get("queued", 0)) for value in files.values() if isinstance(value, dict)
            ),
            "skipped_non_e3": sum(
                1 for value in files.values() if isinstance(value, dict) and value.get("status") == "skipped_non_e3"
            ),
            "failed": sum(
                1 for value in files.values() if isinstance(value, dict) and value.get("status") == "failed"
            ),
        }
        state["status"] = "complete" if remaining == 0 else "partial"
        state["completed_at"] = now_iso() if remaining == 0 else None
        state["updated_at"] = now_iso()
        state["totals"] = aggregate_totals
        state["remaining_files"] = remaining
        if not dry_run:
            write_json_atomic(state_path(base), state)
            if auto_upload_enabled():
                drain_result = drain_queue()
                state["last_drain"] = drain_result
                drain_problem = any(
                    int(drain_result.get(key, 0)) > 0
                    for key in ("failed", "dead_lettered", "remaining")
                )
                if drain_problem:
                    state["status"] = "partial"
                    state["completed_at"] = None
                state["updated_at"] = now_iso()
                write_json_atomic(state_path(base), state)
            if reporting_remote:
                report_status(state, base=base, repo_remote=reporting_remote)
        return {"version": BACKFILL_VERSION, **totals, "remaining": remaining, "status": state["status"]}


def import_transcript(
    path: Path,
    *,
    base: Path,
    dry_run: bool,
    repo_by_cwd: dict[str, str] | None = None,
) -> JsonDict:
    meta = read_session_meta(path)
    cwd = string_value(meta.get("cwd"))
    git = meta.get("git") if isinstance(meta.get("git"), dict) else {}
    remote = (
        string_value(git.get("repository_url"))
        or ((repo_by_cwd or {}).get(cwd) if cwd else None)
        or (git_origin_remote(cwd) if cwd else None)
    )
    if not remote_belongs_to_org(remote, ALLOWED_GITHUB_ORG):
        return {"status": "skipped_non_e3", "queued": 0}

    session_id = string_value(meta.get("session_id")) or string_value(meta.get("id")) or session_id_from_filename(path)
    thread_id = sha256_hex(str(path.resolve()))
    fallback_created_at = string_value(meta.get("timestamp")) or datetime.fromtimestamp(
        path.stat().st_mtime,
        tz=timezone.utc,
    ).isoformat()
    queued = 0
    prefer_event_user = transcript_has_user_events(path)
    for line_number, envelope in iter_transcript(path):
        parsed = historical_message(
            envelope,
            prefer_event_user=prefer_event_user,
            fallback_created_at=fallback_created_at,
        )
        if parsed is None:
            continue
        role, content, turn_id, created_at = parsed
        seq = historical_sequence(path, line_number)
        record_id = deterministic_uuid(f"backfill-v{BACKFILL_VERSION}:{path.resolve()}:{line_number}:message")
        content_bytes = content.encode("utf-8")
        storage_path = f"users/local/sessions/{safe_segment(session_id)}/messages/{seq}-{role}.json"
        metadata: JsonDict = {
            "cwd": cwd,
            "repo_remote": remote,
            "transcript_path": str(path.resolve()),
            "source": "historical_transcript",
            "backfill_version": BACKFILL_VERSION,
            "source_line": line_number,
        }
        branch = string_value(git.get("branch"))
        commit = string_value(git.get("commit_hash"))
        if branch:
            metadata["git_branch"] = branch
        if commit:
            metadata["git_commit"] = commit
        detail: JsonDict = {
            "id": record_id,
            "session_id": session_id,
            "thread_id": thread_id,
            "turn_id": turn_id,
            "seq": seq,
            "role": role,
            "content": content,
            "content_sha256": hashlib.sha256(content_bytes).hexdigest(),
            "content_byte_size": len(content_bytes),
            "hook_event_name": "HistoricalBackfill",
            "created_at": created_at,
            "metadata": metadata,
        }
        record: JsonDict = {
            key: value
            for key, value in detail.items()
            if key not in {"content"}
        }
        record.update({
            "type": "message",
            "storage_bucket": bucket_name(),
            "storage_path": storage_path,
            "local_content_path": storage_path,
            "content_excerpt": content_bytes[:EXCERPT_BYTES].decode("utf-8", errors="replace"),
            "uploaded_at": None,
        })
        queued += 1
        if not dry_run:
            write_json_atomic(base / storage_path, detail)
            enqueue_record(base, record)
    return {"status": "complete", "queued": queued, "repo_remote": remote, "session_id": session_id}


def iter_transcript(path: Path) -> Iterator[tuple[int, JsonDict]]:
    with path.open(encoding="utf-8") as handle:
        for line_number, line in enumerate(handle):
            if not line.strip():
                continue
            try:
                loaded = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(loaded, dict):
                continue
            yield line_number, loaded


def transcript_has_user_events(path: Path) -> bool:
    return any(
        envelope.get("type") == "event_msg"
        and isinstance(envelope.get("payload"), dict)
        and envelope["payload"].get("type") == "user_message"
        for _, envelope in iter_transcript(path)
    )


def known_repositories(paths: list[Path]) -> dict[str, str]:
    candidates: dict[str, set[str]] = {}
    for path in paths:
        try:
            meta = read_session_meta(path)
        except OSError:
            continue
        cwd = string_value(meta.get("cwd"))
        git = meta.get("git") if isinstance(meta.get("git"), dict) else {}
        remote = string_value(git.get("repository_url"))
        if cwd and remote:
            candidates.setdefault(cwd, set()).add(remote)
    return {
        cwd: next(iter(remotes))
        for cwd, remotes in candidates.items()
        if len(remotes) == 1
        and remote_belongs_to_org(next(iter(remotes)), ALLOWED_GITHUB_ORG)
    }


def read_session_meta(path: Path) -> JsonDict:
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            if not line.strip():
                continue
            try:
                loaded = json.loads(line)
            except json.JSONDecodeError:
                continue
            if (
                isinstance(loaded, dict)
                and loaded.get("type") == "session_meta"
                and isinstance(loaded.get("payload"), dict)
            ):
                return loaded["payload"]
    return {}


def historical_message(
    envelope: JsonDict,
    *,
    prefer_event_user: bool = False,
    fallback_created_at: str | None = None,
) -> tuple[str, str, str | None, str] | None:
    if envelope.get("type") == "event_msg":
        payload = envelope.get("payload")
        if not isinstance(payload, dict) or payload.get("type") != "user_message":
            return None
        content = string_value(payload.get("message"))
        if not content:
            return None
        timestamp = string_value(envelope.get("timestamp")) or fallback_created_at or now_iso()
        return "user", content, string_value(payload.get("turn_id")), timestamp
    if envelope.get("type") != "response_item":
        return None
    payload = envelope.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "message":
        return None
    role = payload.get("role")
    if role not in {"user", "assistant"}:
        return None
    if role == "user" and prefer_event_user:
        return None
    phase = string_value(payload.get("phase"))
    if role == "assistant" and phase and phase != "final_answer":
        return None
    content = message_text(payload.get("content"))
    if not content:
        return None
    timestamp = string_value(envelope.get("timestamp")) or fallback_created_at or now_iso()
    return role, content, string_value(payload.get("turn_id")), timestamp


def message_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
        elif isinstance(item, dict) and item.get("type") in {"input_text", "output_text", "text"}:
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return "\n".join(parts)


def deterministic_uuid(value: str) -> str:
    digest = bytearray(hashlib.sha256(value.encode("utf-8")).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x50
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


def historical_sequence(path: Path, line_number: int) -> int:
    digest = hashlib.sha256(
        f"backfill-v{BACKFILL_VERSION}:{path.resolve()}:{line_number}".encode("utf-8")
    ).digest()
    return -(int.from_bytes(digest[:8], "big") % 2_000_000_000 + 1)


def session_id_from_filename(path: Path) -> str:
    stem = path.stem
    candidate = stem.rsplit("-", 5)[-5:]
    joined = "-".join(candidate)
    try:
        return str(uuid.UUID(joined))
    except ValueError:
        return sha256_hex(str(path.resolve()))[:32]


def string_value(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def report_status(state: JsonDict, *, base: Path, repo_remote: str) -> None:
    record = {
        "created_at": now_iso(),
        "metadata": {"cwd": str(codex_home()), "repo_remote": repo_remote},
    }
    payload = {
        "version": 1,
        "kind": "backfill_status",
        "plugin": {"name": "codex-session-logging"},
        "client": client_context(record, base=base),
        "backfill": {
            "version": BACKFILL_VERSION,
            "status": state.get("status"),
            "started_at": state.get("started_at"),
            "completed_at": state.get("completed_at"),
            "updated_at": state.get("updated_at"),
            "remaining_files": state.get("remaining_files"),
            "totals": state.get("totals", {}),
            "metadata": {
                "plugin_version": PLUGIN_VERSION,
                "last_drain": state.get("last_drain", {}),
            },
        },
    }
    try:
        IngestUploader.from_env().post(payload)
    except Exception as exc:  # noqa: BLE001 - status telemetry must not fail the backfill.
        append_jsonl(backfill_dir(base) / "status_errors.jsonl", {"created_at": now_iso(), "error": str(exc)})


if __name__ == "__main__":
    main()
