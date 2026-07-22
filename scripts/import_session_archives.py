#!/usr/bin/env python3
"""Import exported Codex and Claude JSONL archives into queryable Supabase tables."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


JsonObject = dict[str, Any]
IMPORTER_VERSION = 1
ID_NAMESPACE = uuid.UUID("70264f90-c224-55df-877f-af62eed90413")
DEFAULT_BATCH_BYTES = 1_500_000
DEFAULT_BATCH_ROWS = 750
DEFAULT_WORKERS = 4
MAX_EXCERPT_CHARS = 4_000
MAX_JSONB_RECORD_BYTES = 500_000


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Import e3 AI session ZIPs into Supabase for MCP queries.")
    parser.add_argument("--project-ref", required=True)
    parser.add_argument(
        "--archive",
        action="append",
        required=True,
        metavar="USER_ID=ZIP_PATH",
        help="Archive owner UUID and ZIP path; repeat for each archive.",
    )
    parser.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    parser.add_argument("--batch-bytes", type=int, default=DEFAULT_BATCH_BYTES)
    parser.add_argument("--batch-rows", type=int, default=DEFAULT_BATCH_ROWS)
    args = parser.parse_args(argv)

    key = service_role_key(args.project_ref)
    client = RestClient(args.project_ref, key)
    results: list[JsonObject] = []
    for item in args.archive:
        user_id, path = parse_archive_arg(item)
        results.append(
            import_archive(
                path=path,
                user_id=user_id,
                client=client,
                workers=max(1, args.workers),
                batch_bytes=max(50_000, args.batch_bytes),
                batch_rows=max(1, args.batch_rows),
            )
        )
    print(json.dumps(results, indent=2, sort_keys=True))
    return 0


class RestClient:
    def __init__(self, project_ref: str, service_key: str) -> None:
        self.base_url = f"https://{project_ref}.supabase.co/rest/v1"
        self.headers = {
            "apikey": service_key,
            "authorization": f"Bearer {service_key}",
            "content-type": "application/json",
            "prefer": "resolution=merge-duplicates,return=minimal",
            "user-agent": f"e3-session-archive-import/{IMPORTER_VERSION}",
        }

    def upsert(self, table: str, rows: JsonObject | list[JsonObject], *, conflict: str = "id") -> None:
        payload = rows if isinstance(rows, list) else [rows]
        query = urllib.parse.urlencode({"on_conflict": conflict})
        request = urllib.request.Request(
            f"{self.base_url}/{table}?{query}",
            data=json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8"),
            headers=self.headers,
            method="POST",
        )
        for attempt in range(6):
            try:
                with urllib.request.urlopen(request, timeout=180) as response:
                    if 200 <= response.status < 300:
                        return
                    raise RuntimeError(f"Supabase returned HTTP {response.status} for {table}")
            except urllib.error.HTTPError as exc:
                body = exc.read(2_000).decode("utf-8", errors="replace")
                if exc.code not in {408, 429, 500, 502, 503, 504} or attempt == 5:
                    raise RuntimeError(f"Supabase upsert failed for {table}: HTTP {exc.code}: {body}") from exc
            except (urllib.error.URLError, TimeoutError) as exc:
                if attempt == 5:
                    raise RuntimeError(f"Supabase upsert failed for {table}: {exc}") from exc
            time.sleep(min(2**attempt, 20))


def service_role_key(project_ref: str) -> str:
    completed = subprocess.run(
        ["supabase", "projects", "api-keys", "--project-ref", project_ref, "-o", "json"],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("Unable to obtain the Supabase service-role key from the authenticated CLI profile.")
    try:
        rows = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError("Supabase CLI returned invalid API-key JSON.") from exc
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("name") != "service_role":
            continue
        for key in ("api_key", "key", "value"):
            value = row.get(key)
            if isinstance(value, str) and value:
                return value
    raise RuntimeError("The Supabase CLI profile did not return a service-role key.")


def parse_archive_arg(value: str) -> tuple[str, Path]:
    if "=" not in value:
        raise ValueError("--archive must use USER_ID=ZIP_PATH")
    user_id, raw_path = value.split("=", 1)
    normalized_user_id = str(uuid.UUID(user_id.strip()))
    path = Path(raw_path).expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(path)
    return normalized_user_id, path


def import_archive(
    *,
    path: Path,
    user_id: str,
    client: RestClient,
    workers: int,
    batch_bytes: int,
    batch_rows: int,
) -> JsonObject:
    archive_sha = sha256_file(path)
    import_id = stable_uuid(f"import:{user_id}:{archive_sha}")
    started_at = now_iso()
    with zipfile.ZipFile(path) as archive:
        bad_member = archive.testzip()
        if bad_member:
            raise RuntimeError(f"corrupt ZIP member: {bad_member}")
        manifest_name = next((name for name in archive.namelist() if name.endswith("/manifest.json")), None)
        if not manifest_name:
            raise ValueError(f"archive has no manifest.json: {path}")
        manifest = json.loads(archive.read(manifest_name))
        sessions = manifest.get("sessions")
        if not isinstance(sessions, list):
            raise ValueError(f"archive manifest has no sessions array: {path}")

        import_row = {
            "id": import_id,
            "user_id": user_id,
            "archive_sha256": archive_sha,
            "source_filename": path.name,
            "redacted": bool(manifest.get("redacted")),
            "status": "importing",
            "transcript_count": len(sessions),
            "record_count": 0,
            "parsed_record_count": 0,
            "invalid_record_count": 0,
            "manifest": manifest,
            "metadata": {"importer_version": IMPORTER_VERSION, "local_archive_bytes": path.stat().st_size},
            "started_at": started_at,
            "completed_at": None,
            "updated_at": started_at,
        }
        client.upsert("ai_session_imports", import_row)

        totals = {"records": 0, "parsed": 0, "invalid": 0}
        try:
            with BatchedUpserter(
                client,
                workers=workers,
                batch_bytes=batch_bytes,
                batch_rows=batch_rows,
            ) as upserter:
                for index, session in enumerate(sessions, 1):
                    if not isinstance(session, dict):
                        raise ValueError(f"invalid session manifest entry at index {index}")
                    stats = import_transcript(
                        archive=archive,
                        session=session,
                        import_id=import_id,
                        user_id=user_id,
                        client=client,
                        upserter=upserter,
                    )
                    for key in totals:
                        totals[key] += stats[key]
                    if index % 25 == 0 or index == len(sessions):
                        print(
                            f"{path.name}: {index}/{len(sessions)} transcripts, "
                            f"{totals['records']} records queued",
                            file=sys.stderr,
                            flush=True,
                        )
                upserter.flush()
        except BaseException:
            client.upsert(
                "ai_session_imports",
                {
                    **import_row,
                    "status": "failed",
                    "record_count": totals["records"],
                    "parsed_record_count": totals["parsed"],
                    "invalid_record_count": totals["invalid"],
                    "updated_at": now_iso(),
                },
            )
            raise

    completed_at = now_iso()
    client.upsert(
        "ai_session_imports",
        {
            **import_row,
            "status": "complete",
            "record_count": totals["records"],
            "parsed_record_count": totals["parsed"],
            "invalid_record_count": totals["invalid"],
            "completed_at": completed_at,
            "updated_at": completed_at,
        },
    )
    return {
        "archive": str(path),
        "import_id": import_id,
        "user_id": user_id,
        "transcripts": len(sessions),
        **totals,
        "status": "complete",
    }


def import_transcript(
    *,
    archive: zipfile.ZipFile,
    session: JsonObject,
    import_id: str,
    user_id: str,
    client: RestClient,
    upserter: "BatchedUpserter",
) -> JsonObject:
    source_path = require_string(session.get("archive_path"), "session.archive_path")
    platform = require_string(session.get("source"), "session.source")
    if platform not in {"codex", "claude"}:
        raise ValueError(f"unsupported platform: {platform}")
    transcript_id = stable_uuid(f"transcript:{import_id}:{source_path}")
    session_id = optional_string(session.get("session_id"))
    transcript_row: JsonObject = {
        "id": transcript_id,
        "import_id": import_id,
        "user_id": user_id,
        "platform": platform,
        "session_id": session_id,
        "repo_remote": optional_string(session.get("repo_remote")),
        "cwd": optional_string(session.get("cwd")),
        "source_path": source_path,
        "verification": optional_string(session.get("verification")),
        "byte_size": non_negative_int(session.get("bytes")),
        "record_count": 0,
        "parsed_record_count": 0,
        "invalid_record_count": 0,
        "started_at": None,
        "ended_at": None,
        "metadata": {
            "sha256": optional_string(session.get("sha256")),
            "relative_path": optional_string(session.get("relative_path")),
            "redactions": non_negative_int(session.get("redactions")),
        },
        "updated_at": now_iso(),
    }
    client.upsert("ai_session_transcripts", transcript_row)

    stats: JsonObject = {"records": 0, "parsed": 0, "invalid": 0}
    timestamps: list[str] = []
    with archive.open(source_path) as handle:
        for seq, raw_line in enumerate(handle, 1):
            if not raw_line.strip():
                continue
            row = archive_record_row(
                raw_line=raw_line,
                seq=seq,
                transcript_id=transcript_id,
                import_id=import_id,
                user_id=user_id,
                platform=platform,
                session_id=session_id,
            )
            upserter.add(row)
            stats["records"] += 1
            if row["payload"] is None:
                stats["invalid"] += 1
            else:
                stats["parsed"] += 1
            if row["occurred_at"]:
                timestamps.append(row["occurred_at"])

    client.upsert(
        "ai_session_transcripts",
        {
            **transcript_row,
            "record_count": stats["records"],
            "parsed_record_count": stats["parsed"],
            "invalid_record_count": stats["invalid"],
            "started_at": min(timestamps) if timestamps else None,
            "ended_at": max(timestamps) if timestamps else None,
            "updated_at": now_iso(),
        },
    )
    return stats


class BatchedUpserter:
    def __init__(
        self,
        client: RestClient,
        *,
        workers: int,
        batch_bytes: int,
        batch_rows: int,
    ) -> None:
        self.client = client
        self.batch_bytes = batch_bytes
        self.batch_rows = batch_rows
        self.executor = ThreadPoolExecutor(max_workers=workers)
        self.max_pending = workers * 2
        self.pending: set[Future[None]] = set()
        self.rows: list[JsonObject] = []
        self.bytes = 2

    def add(self, row: JsonObject) -> None:
        row_bytes = len(json.dumps(row, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1
        if self.rows and (len(self.rows) >= self.batch_rows or self.bytes + row_bytes > self.batch_bytes):
            self.submit()
        self.rows.append(row)
        self.bytes += row_bytes

    def submit(self) -> None:
        if not self.rows:
            return
        rows = self.rows
        self.rows = []
        self.bytes = 2
        self.pending.add(
            self.executor.submit(self.client.upsert, "ai_session_records", rows, conflict="id")
        )
        if len(self.pending) >= self.max_pending:
            self.reap(first_only=True)

    def reap(self, *, first_only: bool) -> None:
        if not self.pending:
            return
        if first_only:
            done, self.pending = wait(self.pending, return_when=FIRST_COMPLETED)
        else:
            done, self.pending = self.pending, set()
        for future in done:
            future.result()

    def flush(self) -> None:
        self.submit()
        self.reap(first_only=False)

    def __enter__(self) -> "BatchedUpserter":
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            if exc_type is None:
                self.flush()
        finally:
            self.executor.shutdown(wait=True, cancel_futures=exc_type is not None)


def archive_record_row(
    *,
    raw_line: bytes,
    seq: int,
    transcript_id: str,
    import_id: str,
    user_id: str,
    platform: str,
    session_id: str | None,
) -> JsonObject:
    decoded = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
    payload: JsonObject | None
    searchable_payload: JsonObject | None = None
    raw_text: str | None
    parse_error: str | None
    try:
        loaded = json.loads(decoded)
        if not isinstance(loaded, dict):
            raise ValueError("record must be a JSON object")
        if contains_nul(loaded):
            raise ValueError("record contains U+0000 and is preserved as raw JSON text")
        searchable_payload = loaded
        if len(raw_line) > MAX_JSONB_RECORD_BYTES:
            payload = None
            raw_text = decoded
            parse_error = (
                f"record exceeds {MAX_JSONB_RECORD_BYTES} bytes and is preserved as raw JSON text"
            )
        else:
            payload = loaded
            raw_text = None
            parse_error = None
    except (json.JSONDecodeError, ValueError) as exc:
        payload = None
        raw_text = decoded
        parse_error = str(exc)[:500]

    record_type, record_subtype, occurred_at, role, tool_name, excerpt = searchable_fields(
        searchable_payload,
        platform=platform,
    )
    return {
        "id": stable_uuid(f"record:{transcript_id}:{seq}"),
        "import_id": import_id,
        "transcript_id": transcript_id,
        "user_id": user_id,
        "platform": platform,
        "session_id": session_id,
        "seq": seq,
        "record_type": record_type,
        "record_subtype": record_subtype,
        "occurred_at": occurred_at,
        "role": role,
        "tool_name": tool_name,
        "content_excerpt": excerpt,
        "payload": payload,
        "raw_text": raw_text,
        "parse_error": parse_error,
        "updated_at": now_iso(),
    }


def searchable_fields(
    payload: JsonObject | None,
    *,
    platform: str,
) -> tuple[str, str | None, str | None, str | None, str | None, str | None]:
    if payload is None:
        return "invalid_json", None, None, None, None, None
    record_type = optional_string(payload.get("type")) or "unknown"
    nested = payload.get("payload") if isinstance(payload.get("payload"), dict) else {}
    message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
    record_subtype = optional_string(nested.get("type")) if platform == "codex" else optional_string(payload.get("subtype"))
    occurred_at = valid_timestamp(payload.get("timestamp")) or valid_timestamp(nested.get("timestamp"))
    role = optional_string(nested.get("role")) or optional_string(message.get("role"))
    if not role and record_type in {"user", "assistant"}:
        role = record_type
    tool_name = optional_string(nested.get("name"))
    content_source: Any = nested
    if platform == "claude":
        content_source = message.get("content") if "content" in message else payload.get("content")
        blocks = message.get("content") if isinstance(message.get("content"), list) else []
        for block in blocks:
            if isinstance(block, dict) and block.get("type") == "tool_use":
                tool_name = tool_name or optional_string(block.get("name"))
                break
    excerpt = text_excerpt(content_source)
    return record_type, record_subtype, occurred_at, role, tool_name, excerpt


def contains_nul(value: Any) -> bool:
    if isinstance(value, str):
        return "\x00" in value
    if isinstance(value, list):
        return any(contains_nul(item) for item in value)
    if isinstance(value, dict):
        return any(contains_nul(key) or contains_nul(item) for key, item in value.items())
    return False


def text_excerpt(value: Any) -> str | None:
    parts: list[str] = []

    def visit(item: Any, depth: int = 0) -> None:
        if depth > 5 or sum(len(part) for part in parts) >= MAX_EXCERPT_CHARS:
            return
        if isinstance(item, str):
            if item.strip():
                parts.append(item.strip())
            return
        if isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
            return
        if not isinstance(item, dict):
            return
        preferred = ("text", "message", "content", "output", "arguments", "input", "thinking")
        matched = False
        for key in preferred:
            if key in item:
                matched = True
                visit(item[key], depth + 1)
        if not matched:
            for child in item.values():
                visit(child, depth + 1)

    visit(value)
    if not parts:
        return None
    return "\n".join(parts)[:MAX_EXCERPT_CHARS]


def valid_timestamp(value: Any) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    try:
        datetime.fromisoformat(candidate.replace("Z", "+00:00"))
    except ValueError:
        return None
    return candidate


def stable_uuid(value: str) -> str:
    return str(uuid.uuid5(ID_NAMESPACE, value))


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def optional_string(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def require_string(value: Any, field: str) -> str:
    rendered = optional_string(value)
    if not rendered:
        raise ValueError(f"{field} is required")
    return rendered


def non_negative_int(value: Any) -> int:
    return max(0, int(value)) if isinstance(value, (int, float)) else 0


if __name__ == "__main__":
    sys.exit(main())
