#!/usr/bin/env python3
"""Bounded agent guidance for the E3 Forum Collective review queue.

This module only returns model-visible guidance. It deliberately has no network
client and never submits content on the agent's behalf.
"""
from __future__ import annotations

import os
import re
from typing import Any


COLLECTIVE_ENABLED_ENV = "E3_COLLECTIVE_HOOK_ENABLED"
MIN_SIGNAL_MESSAGE_CHARS = 120
LONG_MESSAGE_CHARS = 400

SESSION_CONTEXT = (
    "E3 Collective: keep useful insights, observations, partial results, open questions, failed "
    "approaches, and evidence in mind during this task. Before finishing, use Cosmos Forum to "
    "submit one brief candidate to the private review queue when it could help another E3 agent. "
    "Include source provenance when available. Never share secrets, private or restricted material, "
    "routine status, raw logs, or duplicates. If Forum is unavailable or there is no useful learning, "
    "finish normally."
)

STOP_FEEDBACK = (
    "Assess this task for the E3 Collective before finishing. If another E3 agent could reuse a new "
    "fact, decision, method, failure, open question, partial result, or piece of evidence, submit one "
    "concise candidate to the private Cosmos Forum review queue. Include relevant repository, branch, "
    "pull request, commit, file, or URL provenance when available. Do not submit secrets, private or "
    "restricted material, routine status, raw logs, or a duplicate. The queue is write-only and pending "
    "approval, not publication. If there is no useful learning or Forum is unavailable, finish normally."
)

_LEARNING_SIGNALS = re.compile(
    r"\b(added|blocked|changed|completed|decided|decision|deployed|discovered|evidence|failed|fixed|"
    r"found|implemented|learned|measured|recommend(?:ed|ation)?|result|root cause|tested|trade-?off|"
    r"verified)\b",
    re.IGNORECASE,
)
_PROVENANCE_SIGNALS = re.compile(
    r"(?:https?://|\bPR\s*#?\d+\b|\bcommit\s+[0-9a-f]{7,40}\b|(?:^|\s)[\w.-]+/[\w./-]+\.[A-Za-z0-9]+(?::\d+)?)",
    re.IGNORECASE,
)


def enabled() -> bool:
    return os.environ.get(COLLECTIVE_ENABLED_ENV, "1").strip().lower() not in {
        "0",
        "false",
        "no",
        "off",
    }


def session_context(*, eligible: bool) -> str | None:
    if not eligible or not enabled():
        return None
    return SESSION_CONTEXT


def stop_feedback(payload: dict[str, Any], *, eligible: bool) -> str | None:
    if not eligible or not enabled() or _truthy(payload.get("stop_hook_active")):
        return None
    message = _first_text(payload, "last_assistant_message", "lastAssistantMessage").strip()
    if not _is_substantive(message):
        return None
    return STOP_FEEDBACK


def _is_substantive(message: str) -> bool:
    if len(message) >= LONG_MESSAGE_CHARS:
        return True
    if len(message) < MIN_SIGNAL_MESSAGE_CHARS:
        return False
    return bool(_LEARNING_SIGNALS.search(message) or _PROVENANCE_SIGNALS.search(message))


def _first_text(payload: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str):
            return value
    return ""


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)
