#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any, Iterable
from uuid import UUID


JsonDict = dict[str, Any]


def parse_cumulative_usage(envelope: JsonDict) -> JsonDict | None:
    """Parse one Codex cumulative token-count envelope.

    Codex emits cumulative session totals, not per-turn deltas. Invalid or
    incomplete records are ignored so a malformed rollout line cannot erase a
    previously accepted total.
    """
    payload = envelope.get("payload")
    if (
        envelope.get("type") != "event_msg"
        or not isinstance(payload, dict)
        or payload.get("type") != "token_count"
    ):
        return None
    info = payload.get("info")
    if not isinstance(info, dict):
        return None
    total = info.get("total_token_usage")
    if not isinstance(total, dict):
        return None

    raw_input_tokens = non_negative_int(total.get("input_tokens"))
    raw_output_tokens = non_negative_int(total.get("output_tokens"))
    total_tokens = non_negative_int(total.get("total_tokens"))
    cached_input_tokens = optional_non_negative_int(total, "cached_input_tokens")
    reasoning_output_tokens = optional_non_negative_int(total, "reasoning_output_tokens")
    if (
        raw_input_tokens is None
        or raw_output_tokens is None
        or total_tokens is None
        or cached_input_tokens is None
        or reasoning_output_tokens is None
        or cached_input_tokens > raw_input_tokens
        or reasoning_output_tokens > raw_output_tokens
        or raw_input_tokens + raw_output_tokens != total_tokens
    ):
        return None

    created_at = aware_timestamp(envelope.get("timestamp"))
    if not created_at:
        return None
    usage: JsonDict = {
        "input_tokens": raw_input_tokens - cached_input_tokens,
        "cached_input_tokens": cached_input_tokens,
        "output_tokens": raw_output_tokens - reasoning_output_tokens,
        "reasoning_output_tokens": reasoning_output_tokens,
        "total_tokens": total_tokens,
        "created_at": created_at,
    }
    model_context_window = non_negative_int(info.get("model_context_window"))
    if model_context_window is not None:
        usage["model_context_window"] = model_context_window
    return usage


def latest_cumulative_usage(envelopes: Iterable[JsonDict]) -> JsonDict | None:
    """Return the newest cumulative snapshot, preferring larger equal-time totals."""
    latest: JsonDict | None = None
    for envelope in envelopes:
        parsed = parse_cumulative_usage(envelope)
        if parsed is not None and usage_is_newer(parsed, latest):
            latest = parsed
    return latest


def cumulative_usage_observations(
    envelopes: Iterable[JsonDict],
) -> list[JsonDict]:
    """Return every valid cumulative observation in source order."""
    observations: list[JsonDict] = []
    for envelope in envelopes:
        parsed = parse_cumulative_usage(envelope)
        if parsed is not None:
            observations.append(parsed)
    return observations


def usage_observation_id(session_id: str, usage: JsonDict) -> str:
    """Derive a stable queue id from the stored observation fields."""
    identity = json.dumps(
        [
            session_id,
            canonical_observed_at(str(usage["created_at"])),
            usage["input_tokens"],
            usage["cached_input_tokens"],
            usage["output_tokens"],
            usage["reasoning_output_tokens"],
        ],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    digest = bytearray(hashlib.sha256(identity.encode("utf-8")).digest()[:16])
    digest[6] = (digest[6] & 0x0F) | 0x50
    digest[8] = (digest[8] & 0x3F) | 0x80
    return str(UUID(bytes=bytes(digest)))


def canonical_observed_at(value: str) -> str:
    """Normalize equivalent aware timestamps before deriving observation ids."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value
    if parsed.tzinfo is None:
        return value
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds").replace(
        "+00:00", "Z"
    )


def usage_is_newer(candidate: JsonDict, current: JsonDict | None) -> bool:
    if current is None:
        return True
    candidate_total = non_negative_int(candidate.get("total_tokens"))
    current_total = non_negative_int(current.get("total_tokens"))
    if candidate_total is None:
        return False
    if current_total is None:
        return True
    # Session usage is cumulative. A later observation may repeat the same
    # total, but it must never move an accepted session total backwards.
    if candidate_total < current_total:
        return False
    candidate_time = parsed_timestamp(candidate.get("created_at"))
    current_time = parsed_timestamp(current.get("created_at"))
    if candidate_time is not None and current_time is not None:
        if candidate_time != current_time:
            return candidate_time > current_time
    elif candidate_time is not None:
        return True
    elif current_time is not None:
        return False
    else:
        candidate_text = non_empty_string(candidate.get("created_at")) or ""
        current_text = non_empty_string(current.get("created_at")) or ""
        if candidate_text != current_text:
            return candidate_text > current_text
    return candidate_total >= current_total


def parsed_timestamp(value: object) -> datetime | None:
    text = non_empty_string(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def aware_timestamp(value: object) -> str | None:
    text = non_empty_string(value)
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(
            text[:-1] + "+00:00" if text.endswith("Z") else text
        )
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return text


def non_empty_string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def non_negative_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def optional_non_negative_int(source: JsonDict, key: str) -> int | None:
    if key not in source:
        return 0
    return non_negative_int(source.get(key))
