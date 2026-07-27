#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterator


PLUGIN_SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"
if str(PLUGIN_SCRIPTS) not in sys.path:
    sys.path.insert(0, str(PLUGIN_SCRIPTS))

from rollout_usage import parse_cumulative_usage, usage_is_newer  # noqa: E402


JsonDict = dict[str, Any]
MAX_PARTIAL_LINE_BYTES = 1024 * 1024
DEFAULT_PAGE_SIZE = 500


class ReplayError(RuntimeError):
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

    def create_rollout_replay_snapshot(self) -> tuple[str, int]:
        rows = self.request_json(
            "POST",
            "/rest/v1/rpc/create_codex_rollout_replay_snapshot",
            {},
        )
        if not isinstance(rows, list) or len(rows) != 1:
            raise ReplayError("snapshot RPC response must contain exactly one row")
        row = required_object(rows[0], "snapshot RPC row")
        return (
            required_uuid(row.get("snapshot_id"), "snapshot_id"),
            non_negative_integer(row.get("event_count"), "snapshot event_count"),
        )

    def rollout_replay_snapshot_event_count(self, snapshot_id: str) -> int:
        filters = [
            ("select", "event_count"),
            ("snapshot_id", f"eq.{required_uuid(snapshot_id, 'snapshot_id')}"),
            ("limit", "1"),
        ]
        rows = self.request_json(
            "GET",
            "/rest/v1/codex_rollout_replay_snapshots?"
            + urllib.parse.urlencode(filters),
        )
        if not isinstance(rows, list):
            raise ReplayError("snapshot lookup response must be a JSON array")
        if not rows:
            raise ReplayError(f"rollout replay snapshot {snapshot_id} does not exist")
        row = required_object(rows[0], "snapshot lookup row")
        return non_negative_integer(row.get("event_count"), "snapshot event_count")

    def iter_rollout_events(
        self,
        *,
        page_size: int,
        after_session: str | None,
        snapshot_id: str,
    ) -> Iterator[JsonDict]:
        snapshot_id = required_uuid(snapshot_id, "snapshot_id")
        cursor: tuple[str, str] | None = None
        while True:
            filters = [
                (
                    "select",
                    "id,session_id,user_id,storage_bucket,storage_path,metadata",
                ),
                ("snapshot_id", f"eq.{snapshot_id}"),
                ("order", "session_id.asc,id.asc"),
                ("limit", str(page_size)),
            ]
            if after_session:
                filters.append(("session_id", f"gt.{after_session}"))
            if cursor is not None:
                filters.append(("or", cursor_filter(cursor)))
            path = (
                "/rest/v1/codex_rollout_replay_snapshot_events?"
                + urllib.parse.urlencode(filters)
            )
            rows = self.request_json("GET", path)
            if not isinstance(rows, list):
                raise ReplayError("snapshot events response must be a JSON array")
            for row in rows:
                if not isinstance(row, dict):
                    raise ReplayError("snapshot event row must be a JSON object")
                yield row
            if len(rows) < page_size:
                return
            next_cursor = event_cursor(rows[-1])
            if next_cursor == cursor:
                raise ReplayError("snapshot event keyset cursor did not advance")
            cursor = next_cursor

    def download(self, bucket: str, storage_path: str) -> bytes:
        quoted_path = "/".join(
            urllib.parse.quote(piece, safe="") for piece in storage_path.split("/")
        )
        quoted_bucket = urllib.parse.quote(bucket, safe="")
        return self.request_bytes(
            "GET",
            f"/storage/v1/object/authenticated/{quoted_bucket}/{quoted_path}",
        )

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
        body = None if payload is None else json.dumps(payload).encode("utf-8")
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
            f"{self.url}{path}",
            method=method,
            data=body,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                return response.read()
        except urllib.error.HTTPError as exc:
            raise ReplayError(
                f"Supabase request failed {exc.code} for {safe_endpoint(path)}"
            ) from exc
        except urllib.error.URLError as exc:
            raise ReplayError(
                f"Supabase request failed for {safe_endpoint(path)}: {exc.reason}"
            ) from exc


class UsageJsonlStream:
    def __init__(self) -> None:
        self.partial = b""
        self.skip_until_newline = False
        self.latest: JsonDict | None = None

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
            self._parse_line(line)
        tail = pieces[-1]
        if len(tail) <= MAX_PARTIAL_LINE_BYTES:
            self.partial = tail
        else:
            self.skip_until_newline = True

    def _parse_line(self, line: bytes) -> None:
        if not line.strip() or len(line) > MAX_PARTIAL_LINE_BYTES:
            return
        try:
            envelope = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return
        if not isinstance(envelope, dict):
            return
        usage = parse_cumulative_usage(envelope)
        if usage is not None and usage_is_newer(usage, self.latest):
            self.latest = usage


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
            value = parsed["default"].strip()
            if value:
                return value
    raise ReplayError("SUPABASE_SERVICE_ROLE_KEY or SUPABASE_SECRET_KEYS.default is required")


def reprocess_rollout_usage(
    client: SupabaseAdminClient,
    *,
    apply: bool = False,
    page_size: int = DEFAULT_PAGE_SIZE,
    after_session: str | None = None,
    snapshot_id: str | None = None,
) -> JsonDict:
    if after_session is not None and snapshot_id is None:
        raise ReplayError("--after-session requires --snapshot-id")
    if snapshot_id is None:
        snapshot_id, snapshot_event_count = client.create_rollout_replay_snapshot()
    else:
        snapshot_id = required_uuid(snapshot_id, "snapshot_id")
        snapshot_event_count = client.rollout_replay_snapshot_event_count(snapshot_id)

    event_count = 0
    generation_count = 0
    session_count = 0
    sessions_with_usage = 0
    rpc_calls = 0
    errors: list[str] = []
    resume_after_session = after_session
    checkpoint_blocked = False
    current_session: str | None = None
    current_rows: list[JsonDict] = []

    def process_current_session() -> None:
        nonlocal generation_count, session_count, sessions_with_usage
        nonlocal rpc_calls, resume_after_session, checkpoint_blocked
        if current_session is None:
            return
        session_count += 1
        result = reprocess_session(
            client,
            session_id=current_session,
            rows=current_rows,
        )
        generation_count += result["generations"]
        if result["errors"]:
            errors.extend(result["errors"])
            checkpoint_blocked = True
            return
        usage = result.get("usage")
        if usage is not None:
            sessions_with_usage += 1
            if apply:
                try:
                    client.upsert_usage(result["parameters"])
                except ReplayError as exc:
                    errors.append(str(exc))
                    checkpoint_blocked = True
                    return
                rpc_calls += 1
        if not checkpoint_blocked:
            resume_after_session = current_session

    for row in client.iter_rollout_events(
        page_size=max(1, page_size),
        after_session=after_session,
        snapshot_id=snapshot_id,
    ):
        session_id, _generation = chunk_identity(row)
        if current_session is not None and session_id != current_session:
            process_current_session()
            current_rows = []
        current_session = session_id
        current_rows.append(row)
        event_count += 1
    process_current_session()

    return {
        "mode": "apply" if apply else "dry-run",
        "events": event_count,
        "generations": generation_count,
        "sessions": session_count,
        "sessions_with_usage": sessions_with_usage,
        "rpc_calls": rpc_calls,
        "errors": errors,
        "resume_after_session": resume_after_session,
        "snapshot_id": snapshot_id,
        "snapshot_event_count": snapshot_event_count,
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
            raise ReplayError("session-group ordering changed during replay")
        by_generation[generation].append(row)

    latest: tuple[str, JsonDict, str] | None = None
    errors: list[str] = []
    for generation, generation_rows in sorted(by_generation.items()):
        try:
            user_id, usage = usage_from_generation(
                client,
                session_id=session_id,
                generation=generation,
                rows=generation_rows,
            )
        except ReplayError as exc:
            errors.append(str(exc))
            continue
        if usage is not None and (
            latest is None or usage_is_newer(usage, latest[1])
        ):
            latest = (user_id, usage, generation)

    result: JsonDict = {
        "generations": len(by_generation),
        "errors": errors,
        "usage": latest[1] if latest is not None else None,
    }
    if latest is not None and not errors:
        user_id, usage, generation = latest
        result["parameters"] = usage_rpc_parameters(
            session_id=session_id,
            user_id=user_id,
            generation=generation,
            usage=usage,
        )
    return result


def event_cursor(row: object) -> tuple[str, str]:
    event = required_object(row, "rollout event")
    return (
        required_string(event.get("session_id"), "cursor session_id"),
        required_uuid(event.get("id"), "cursor id"),
    )


def cursor_filter(cursor: tuple[str, str]) -> str:
    session_id, event_id = cursor
    return (
        f"(session_id.gt.{session_id},"
        f"and(session_id.eq.{session_id},"
        f"id.gt.{event_id}))"
    )


def chunk_identity(row: JsonDict) -> tuple[str, str]:
    session_id = required_string(row.get("session_id"), "session_id")
    metadata = required_object(row.get("metadata"), f"session {session_id} metadata")
    generation = required_string(
        metadata.get("file_generation"),
        f"session {session_id} file_generation",
    )
    return session_id, generation


def usage_from_generation(
    client: SupabaseAdminClient,
    *,
    session_id: str,
    generation: str,
    rows: list[JsonDict],
) -> tuple[str, JsonDict | None]:
    chunks = [validated_chunk(row, session_id, generation) for row in rows]
    chunks.sort(key=lambda chunk: (chunk["start_offset"], chunk["end_offset"]))
    expected_offset = 0
    user_id: str | None = None
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
        if user_id is not None and user_id != chunk["user_id"]:
            raise generation_error(session_id, generation, "multiple user ids")
        user_id = chunk["user_id"]
        content = client.download(chunk["storage_bucket"], chunk["storage_path"])
        if len(content) != chunk["content_byte_size"]:
            raise generation_error(
                session_id,
                generation,
                f"byte-size mismatch at offset {chunk['start_offset']}",
            )
        if hashlib.sha256(content).hexdigest() != chunk["content_sha256"]:
            raise generation_error(
                session_id,
                generation,
                f"SHA-256 mismatch at offset {chunk['start_offset']}",
            )
        stream.feed(content)
        expected_offset = chunk["end_offset"]
    if user_id is None:
        raise generation_error(session_id, generation, "has no chunks")
    return user_id, stream.latest


def validated_chunk(row: JsonDict, session_id: str, generation: str) -> JsonDict:
    metadata = required_object(row.get("metadata"), f"session {session_id} metadata")
    start = non_negative_integer(
        metadata.get("start_offset"),
        f"session {session_id} start_offset",
    )
    end = non_negative_integer(
        metadata.get("end_offset"),
        f"session {session_id} end_offset",
    )
    size = non_negative_integer(
        metadata.get("content_byte_size"),
        f"session {session_id} content_byte_size",
    )
    if end <= start or size != end - start:
        raise generation_error(session_id, generation, "invalid offset range or byte size")
    digest = required_string(
        metadata.get("content_sha256"),
        f"session {session_id} content_sha256",
    )
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise generation_error(session_id, generation, "invalid SHA-256 metadata")
    return {
        "user_id": required_string(row.get("user_id"), f"session {session_id} user_id"),
        "storage_bucket": required_string(
            row.get("storage_bucket"),
            f"session {session_id} storage_bucket",
        ),
        "storage_path": required_string(
            row.get("storage_path"),
            f"session {session_id} storage_path",
        ),
        "start_offset": start,
        "end_offset": end,
        "content_byte_size": size,
        "content_sha256": digest,
    }


def usage_rpc_parameters(
    *,
    session_id: str,
    user_id: str,
    generation: str,
    usage: JsonDict,
) -> JsonDict:
    return {
        "p_session_id": session_id,
        "p_user_id": user_id,
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


def required_string(value: object, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReplayError(f"{name} must be a non-empty string")
    return value


def required_object(value: object, name: str) -> JsonDict:
    if not isinstance(value, dict):
        raise ReplayError(f"{name} must be an object")
    return value


def required_uuid(value: object, name: str) -> str:
    text = required_string(value, name)
    try:
        parsed = uuid.UUID(text)
    except ValueError as exc:
        raise ReplayError(f"{name} must be a canonical UUID") from exc
    canonical = str(parsed)
    if text != canonical:
        raise ReplayError(f"{name} must be a canonical UUID")
    return canonical


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
        description="Reprocess cumulative usage from stored rollout chunks (dry-run by default).",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Write monotonic usage snapshots through the service-role-only RPC.",
    )
    parser.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    parser.add_argument(
        "--after-session",
        help="Resume after this session id within --snapshot-id.",
    )
    parser.add_argument(
        "--snapshot-id",
        help=(
            "Reuse the immutable snapshot_id from an interrupted run; omit it "
            "to materialize a fresh transaction-visible rollout catalog."
        ),
    )
    args = parser.parse_args(argv)
    try:
        result = reprocess_rollout_usage(
            SupabaseAdminClient.from_env(),
            apply=args.apply,
            page_size=args.page_size,
            after_session=args.after_session,
            snapshot_id=args.snapshot_id,
        )
    except ReplayError as exc:
        print(json.dumps({"error": str(exc)}, sort_keys=True))
        raise SystemExit(1) from exc
    print(json.dumps(result, sort_keys=True))
    if result["errors"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
