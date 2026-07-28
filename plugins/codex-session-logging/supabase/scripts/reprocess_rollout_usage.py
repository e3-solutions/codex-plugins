#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator


PLUGIN_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(PLUGIN_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(PLUGIN_SCRIPTS))

from rollout_usage import (  # noqa: E402
    parse_cumulative_usage,
    usage_is_newer,
    usage_observation_id,
)


JsonDict = dict[str, Any]
MAX_PARTIAL_LINE_BYTES = 1024 * 1024
MAX_EVENTS_PER_SESSION = 5000
DEFAULT_PAGE_SIZE = 200
DEFAULT_LOOKBACK_HOURS = 72
DEFAULT_MAX_SESSIONS = 500
DEFAULT_WORKERS = 1
MAX_WORKERS = 4
STORAGE_GET_ATTEMPTS = 3
STORAGE_GET_BACKOFF_SECONDS = (0.25, 0.5)
REPLAY_CHUNK_METADATA_FIELDS = (
    "file_generation",
    "start_offset",
    "end_offset",
    "content_byte_size",
    "content_sha256",
)


class ReplayError(RuntimeError):
    pass


class TransientRequestError(ReplayError):
    pass


class SupabaseAdminClient:
    def __init__(self, url: str, key: str) -> None:
        self.url = url.rstrip("/")
        self.key = key

    @classmethod
    def from_env(cls) -> "SupabaseAdminClient":
        url = os.environ.get("SUPABASE_URL", "").strip()
        if not url:
            raise ReplayError("SUPABASE_URL is required")
        return cls(url, service_role_key())

    def iter_rollout_events(
        self,
        *,
        page_size: int,
        since: str,
        cutoff: str,
        after_session: str | None,
    ) -> Iterator[JsonDict]:
        cursor: tuple[str, str] | None = None
        while True:
            filters = [
                ("select", "id,session_id,storage_bucket,storage_path,metadata"),
                ("event_type", "eq.rollout_chunk"),
                ("created_at", f"gte.{since}"),
                ("created_at", f"lte.{cutoff}"),
                ("order", "session_id.asc,id.asc"),
                ("limit", str(page_size)),
            ]
            if after_session:
                filters.append(("session_id", f"gt.{after_session}"))
            if cursor:
                filters.append(("or", cursor_filter(cursor)))
            rows = self.request_json(
                "GET",
                "/rest/v1/codex_session_events?" + urllib.parse.urlencode(filters),
            )
            if not isinstance(rows, list):
                raise ReplayError("rollout event response must be a JSON array")
            for row in rows:
                if not isinstance(row, dict):
                    raise ReplayError("rollout event row must be a JSON object")
                yield row
            if len(rows) < page_size:
                return
            next_cursor = event_cursor(rows[-1])
            if next_cursor == cursor:
                raise ReplayError("rollout event keyset cursor did not advance")
            cursor = next_cursor

    def download(self, bucket: str, storage_path: str) -> bytes:
        quoted_path = "/".join(
            urllib.parse.quote(piece, safe="") for piece in storage_path.split("/")
        )
        path = (
            "/storage/v1/object/authenticated/"
            f"{urllib.parse.quote(bucket, safe='')}/{quoted_path}"
        )
        for attempt in range(STORAGE_GET_ATTEMPTS):
            try:
                return self.request_bytes("GET", path)
            except TransientRequestError:
                if attempt + 1 == STORAGE_GET_ATTEMPTS:
                    raise
                time.sleep(STORAGE_GET_BACKOFF_SECONDS[attempt])
        raise AssertionError("unreachable")

    def upsert_usage(self, parameters: JsonDict) -> None:
        self.request_json(
            "POST",
            "/rest/v1/rpc/upsert_codex_session_usage_latest",
            parameters,
        )

    def request_json(
        self,
        method: str,
        path: str,
        payload: JsonDict | None = None,
    ) -> object:
        body = None if payload is None else json.dumps(payload).encode()
        raw = self.request_bytes(
            method,
            path,
            body=body,
            content_type="application/json" if body is not None else None,
        )
        if not raw:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ReplayError(f"non-JSON response for {safe_endpoint(path)}") from exc

    def request_bytes(
        self,
        method: str,
        path: str,
        *,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> bytes:
        headers = {"apikey": self.key, "accept": "application/json"}
        if self.key.startswith("eyJ"):
            headers["authorization"] = f"Bearer {self.key}"
        if content_type:
            headers["content-type"] = content_type
        request = urllib.request.Request(
            f"{self.url}{path}", method=method, data=body, headers=headers
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            error_type = TransientRequestError if 500 <= exc.code < 600 else ReplayError
            raise error_type(
                f"Supabase request failed {exc.code} for {safe_endpoint(path)}"
            ) from exc
        except urllib.error.URLError as exc:
            error_type = (
                TransientRequestError
                if isinstance(exc.reason, (ConnectionError, TimeoutError))
                else ReplayError
            )
            raise error_type(
                f"Supabase request failed for {safe_endpoint(path)}: {exc.reason}"
            ) from exc
        except (ConnectionError, TimeoutError) as exc:
            raise TransientRequestError(
                f"Supabase request failed for {safe_endpoint(path)}"
            ) from exc


class UsageJsonlStream:
    def __init__(self) -> None:
        self.partial = b""
        self.skip_until_newline = False
        self.observations: list[JsonDict] = []

    def feed(self, content: bytes) -> None:
        if self.skip_until_newline:
            newline = content.find(b"\n")
            if newline < 0:
                return
            content = content[newline + 1 :]
            self.skip_until_newline = False

        pieces = (self.partial + content).split(b"\n")
        self.partial = b""
        for line in pieces[:-1]:
            self._accept_line(line)
        tail = pieces[-1]
        if len(tail) <= MAX_PARTIAL_LINE_BYTES:
            self.partial = tail
        else:
            self.skip_until_newline = True

    def finish(self) -> list[JsonDict]:
        """Parse one complete final JSON value even without a newline."""
        if not self.skip_until_newline:
            self._accept_line(self.partial)
        self.partial = b""
        return self.observations

    def _accept_line(self, line: bytes) -> None:
        if not line.strip() or len(line) > MAX_PARTIAL_LINE_BYTES:
            return
        try:
            envelope = json.loads(line.decode())
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if isinstance(envelope, dict):
            usage = parse_cumulative_usage(envelope)
            if usage is not None:
                self.observations.append(usage)


def service_role_key() -> str:
    direct = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "").strip()
    if direct:
        return direct
    secret_keys = os.environ.get("SUPABASE_SECRET_KEYS", "").strip()
    if secret_keys:
        try:
            parsed = json.loads(secret_keys)
        except json.JSONDecodeError as exc:
            raise ReplayError("SUPABASE_SECRET_KEYS must be valid JSON") from exc
        if isinstance(parsed, dict) and isinstance(parsed.get("default"), str):
            key = parsed["default"].strip()
            if key:
                return key
    raise ReplayError("SUPABASE_SERVICE_ROLE_KEY or SUPABASE_SECRET_KEYS.default is required")


def reprocess_rollout_usage(
    client: SupabaseAdminClient,
    *,
    apply: bool = False,
    page_size: int = DEFAULT_PAGE_SIZE,
    lookback_hours: int = DEFAULT_LOOKBACK_HOURS,
    max_sessions: int = DEFAULT_MAX_SESSIONS,
    after_session: str | None = None,
    cutoff: str | None = None,
    workers: int = DEFAULT_WORKERS,
) -> JsonDict:
    if page_size < 1 or lookback_hours < 1 or max_sessions < 1:
        raise ReplayError("page size, lookback hours, and max sessions must be positive")
    if workers < 1 or workers > MAX_WORKERS:
        raise ReplayError(f"workers must be between 1 and {MAX_WORKERS}")
    cutoff_time = parse_timestamp(cutoff) if cutoff else datetime.now(timezone.utc)
    cutoff_text = iso_timestamp(cutoff_time)
    since_text = iso_timestamp(cutoff_time - timedelta(hours=lookback_hours))

    event_count = generation_count = session_count = sessions_with_usage = rpc_calls = 0
    observation_count = 0
    legacy_events_quarantined = legacy_sessions_quarantined = 0
    errors: list[str] = []
    resume_after_session = after_session
    current_session: str | None = None
    current_rows: list[JsonDict] = []
    current_legacy_events = 0
    current_overflow = False
    truncated = False
    stopped = False
    pending_sessions: list[tuple[str, list[JsonDict], int, bool]] = []
    executor = ThreadPoolExecutor(max_workers=workers) if workers > 1 else None

    def queue_current() -> None:
        if current_session is not None:
            pending_sessions.append(
                (
                    current_session,
                    list(current_rows),
                    current_legacy_events,
                    current_overflow,
                )
            )

    def process_pending() -> bool:
        nonlocal generation_count, session_count, sessions_with_usage, rpc_calls
        nonlocal observation_count
        nonlocal legacy_events_quarantined, legacy_sessions_quarantined
        nonlocal resume_after_session
        if not pending_sessions:
            return True
        futures: list[Future[JsonDict] | None] = []
        for session_id, rows, legacy_events, overflow in pending_sessions:
            if legacy_events or overflow or executor is None:
                futures.append(None)
            else:
                futures.append(
                    executor.submit(
                        reprocess_session,
                        client,
                        session_id=session_id,
                        rows=rows,
                    )
                )

        for (session_id, rows, legacy_events, overflow), future in zip(
            pending_sessions, futures
        ):
            session_count += 1
            if legacy_events:
                legacy_events_quarantined += legacy_events
                legacy_sessions_quarantined += 1
                errors.append(
                    f"session {session_id} contains {legacy_events} "
                    "legacy rollout events without replay metadata"
                )
                resume_after_session = session_id
                continue
            if overflow:
                errors.append(
                    f"session {session_id} exceeds {MAX_EVENTS_PER_SESSION} rollout events"
                )
                return False
            result = (
                future.result()
                if future is not None
                else reprocess_session(client, session_id=session_id, rows=rows)
            )
            generation_count += result["generations"]
            if result["errors"]:
                errors.extend(result["errors"])
                return False
            if result["usage"] is not None:
                sessions_with_usage += 1
                observation_count += len(result["parameters"])
                if apply:
                    for parameters in result["parameters"]:
                        try:
                            client.upsert_usage(parameters)
                        except ReplayError as exc:
                            errors.append(str(exc))
                            return False
                        rpc_calls += 1
            resume_after_session = session_id
        pending_sessions.clear()
        return True

    try:
        for row in client.iter_rollout_events(
            page_size=page_size,
            since=since_text,
            cutoff=cutoff_text,
            after_session=after_session,
        ):
            session_id = event_session_id(row)
            if current_session is not None and session_id != current_session:
                queue_current()
                if (
                    len(pending_sessions) >= workers
                    or session_count + len(pending_sessions) >= max_sessions
                ):
                    if not process_pending():
                        stopped = True
                        break
                if session_count >= max_sessions:
                    truncated = True
                    stopped = True
                    break
                current_rows = []
                current_legacy_events = 0
                current_overflow = False
            current_session = session_id
            event_count += 1
            if is_legacy_rollout_event(row):
                current_legacy_events += 1
            elif len(current_rows) < MAX_EVENTS_PER_SESSION:
                current_rows.append(row)
            else:
                current_overflow = True
        if not stopped:
            queue_current()
            process_pending()
    finally:
        if executor is not None:
            executor.shutdown(wait=True)

    return {
        "mode": "apply" if apply else "dry-run",
        "since": since_text,
        "cutoff": cutoff_text,
        "events": event_count,
        "generations": generation_count,
        "sessions": session_count,
        "sessions_with_usage": sessions_with_usage,
        "observations": observation_count,
        "rpc_calls": rpc_calls,
        "legacy_events_quarantined": legacy_events_quarantined,
        "legacy_sessions_quarantined": legacy_sessions_quarantined,
        "errors": errors,
        "resume_after_session": resume_after_session,
        "truncated": truncated,
    }


def reprocess_session(
    client: SupabaseAdminClient,
    *,
    session_id: str,
    rows: list[JsonDict],
) -> JsonDict:
    by_generation: dict[str, list[JsonDict]] = defaultdict(list)
    for row in rows:
        row_session, generation = chunk_identity(row)
        if row_session != session_id:
            raise ReplayError("session grouping changed during replay")
        by_generation[generation].append(row)

    latest: tuple[JsonDict, str] | None = None
    observations: dict[str, tuple[JsonDict, str]] = {}
    errors: list[str] = []
    for generation, generation_rows in sorted(by_generation.items()):
        try:
            generation_usage = usage_from_generation(
                client,
                session_id=session_id,
                generation=generation,
                rows=generation_rows,
            )
        except ReplayError as exc:
            errors.append(str(exc))
            continue
        for usage in generation_usage:
            observation_id = usage_observation_id(session_id, usage)
            observations.setdefault(observation_id, (usage, generation))
            if latest is None or usage_is_newer(usage, latest[0]):
                latest = (usage, generation)

    result: JsonDict = {
        "generations": len(by_generation),
        "errors": errors,
        "usage": latest[0] if latest else None,
    }
    if latest and not errors:
        result["parameters"] = [
            usage_rpc_parameters(
                session_id=session_id,
                generation=generation,
                usage=usage,
            )
            for _observation_id, (usage, generation) in sorted(
                observations.items(),
                key=lambda item: (str(item[1][0]["created_at"]), item[0]),
            )
        ]
    else:
        result["parameters"] = []
    return result


def usage_from_generation(
    client: SupabaseAdminClient,
    *,
    session_id: str,
    generation: str,
    rows: list[JsonDict],
) -> list[JsonDict]:
    chunks = [validated_chunk(row, session_id, generation) for row in rows]
    chunks.sort(key=lambda chunk: (chunk["start_offset"], chunk["end_offset"]))
    expected_offset = 0
    stream = UsageJsonlStream()
    seen_ranges: dict[tuple[int, int], str] = {}
    for chunk in chunks:
        offset_range = (chunk["start_offset"], chunk["end_offset"])
        previous_hash = seen_ranges.get(offset_range)
        if previous_hash is not None:
            if previous_hash != chunk["content_sha256"]:
                raise generation_error(session_id, generation, "conflicting duplicate range")
            continue
        seen_ranges[offset_range] = chunk["content_sha256"]
        if chunk["start_offset"] != expected_offset:
            raise generation_error(
                session_id,
                generation,
                f"non-contiguous offset at {chunk['start_offset']}, expected {expected_offset}",
            )
        content = client.download(chunk["storage_bucket"], chunk["storage_path"])
        if len(content) != chunk["content_byte_size"]:
            raise generation_error(session_id, generation, "stored byte size does not match")
        if hashlib.sha256(content).hexdigest() != chunk["content_sha256"]:
            raise generation_error(session_id, generation, "stored SHA-256 does not match")
        stream.feed(content)
        expected_offset = chunk["end_offset"]
    return stream.finish()


def validated_chunk(row: JsonDict, session_id: str, generation: str) -> JsonDict:
    metadata = required_object(row.get("metadata"), f"session {session_id} metadata")
    start = non_negative_integer(metadata.get("start_offset"), "start_offset")
    end = non_negative_integer(metadata.get("end_offset"), "end_offset")
    size = non_negative_integer(metadata.get("content_byte_size"), "content_byte_size")
    if end <= start or size != end - start:
        raise generation_error(session_id, generation, "invalid offset range or byte size")
    digest = required_string(metadata.get("content_sha256"), "content_sha256")
    if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
        raise generation_error(session_id, generation, "invalid SHA-256 metadata")
    return {
        "storage_bucket": required_string(row.get("storage_bucket"), "storage_bucket"),
        "storage_path": required_string(row.get("storage_path"), "storage_path"),
        "start_offset": start,
        "end_offset": end,
        "content_byte_size": size,
        "content_sha256": digest,
    }


def usage_rpc_parameters(*, session_id: str, generation: str, usage: JsonDict) -> JsonDict:
    return {
        "p_session_id": session_id,
        "p_input_tokens": usage["input_tokens"],
        "p_cached_input_tokens": usage["cached_input_tokens"],
        "p_output_tokens": usage["output_tokens"],
        "p_reasoning_output_tokens": usage["reasoning_output_tokens"],
        "p_total_tokens": usage["total_tokens"],
        "p_model_context_window": usage.get("model_context_window"),
        "p_observed_at": usage["created_at"],
        "p_metadata": {
            "source": "rollout_sync_usage_reprocess",
            "file_generation": generation,
        },
    }


def event_cursor(row: object) -> tuple[str, str]:
    event = required_object(row, "rollout event")
    return required_string(event.get("session_id"), "session_id"), required_string(
        event.get("id"), "id"
    )


def cursor_filter(cursor: tuple[str, str]) -> str:
    session_id, event_id = cursor
    return (
        f"(session_id.gt.{session_id},"
        f"and(session_id.eq.{session_id},id.gt.{event_id}))"
    )


def chunk_identity(row: JsonDict) -> tuple[str, str]:
    session_id = event_session_id(row)
    metadata = required_object(row.get("metadata"), f"session {session_id} metadata")
    return session_id, required_string(metadata.get("file_generation"), "file_generation")


def event_session_id(row: JsonDict) -> str:
    return required_string(row.get("session_id"), "session_id")


def is_legacy_rollout_event(row: JsonDict) -> bool:
    metadata = row.get("metadata")
    return isinstance(metadata, dict) and all(
        field not in metadata for field in REPLAY_CHUNK_METADATA_FIELDS
    )


def parse_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00" if value.endswith("Z") else value)
    except ValueError as exc:
        raise ReplayError("--cutoff must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso_timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReplayError(f"{name} must be a non-empty string")
    return value


def required_object(value: object, name: str) -> JsonDict:
    if not isinstance(value, dict):
        raise ReplayError(f"{name} must be an object")
    return value


def non_negative_integer(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ReplayError(f"{name} must be a non-negative integer")
    return value


def generation_error(session_id: str, generation: str, message: str) -> ReplayError:
    return ReplayError(f"session {session_id} generation {generation}: {message}")


def safe_endpoint(path: str) -> str:
    return path.split("?", 1)[0]


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(
        description="Repair recent cumulative usage from stored rollout chunks (dry-run by default)."
    )
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument("--lookback-hours", type=int, default=DEFAULT_LOOKBACK_HOURS)
    parser.add_argument("--max-sessions", type=int, default=DEFAULT_MAX_SESSIONS)
    parser.add_argument(
        "--workers",
        type=int,
        default=DEFAULT_WORKERS,
        help=f"Concurrent session readers (1-{MAX_WORKERS}; default: 1).",
    )
    parser.add_argument("--after-session", help="Resume after this completed session id.")
    parser.add_argument(
        "--cutoff",
        help="Fixed ISO-8601 catalog watermark; reuse the reported value when resuming.",
    )
    args = parser.parse_args(argv)
    try:
        result = reprocess_rollout_usage(
            SupabaseAdminClient.from_env(),
            apply=args.apply,
            page_size=args.page_size,
            lookback_hours=args.lookback_hours,
            max_sessions=args.max_sessions,
            after_session=args.after_session,
            cutoff=args.cutoff,
            workers=args.workers,
        )
    except ReplayError as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        raise SystemExit(1) from exc
    print(json.dumps(result, sort_keys=True))
    if result["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
