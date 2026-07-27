#!/usr/bin/env python3
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable


JsonDict = dict[str, Any]


def parse_cumulative_usage(
    envelope: JsonDict,
    fallback_created_at: str | None = None,
) -> JsonDict | None:
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

    created_at = non_empty_string(envelope.get("timestamp")) or fallback_created_at
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


def latest_cumulative_usage(
    envelopes: Iterable[JsonDict],
    fallback_created_at: str | None = None,
) -> JsonDict | None:
    """Return the newest cumulative snapshot, preferring larger equal-time totals."""
    latest: JsonDict | None = None
    for envelope in envelopes:
        parsed = parse_cumulative_usage(
            envelope,
            fallback_created_at=fallback_created_at,
        )
        if parsed is not None and usage_is_newer(parsed, latest):
            latest = parsed
    return latest


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
