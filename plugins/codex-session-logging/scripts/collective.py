#!/usr/bin/env python3
"""Bounded agent guidance for the E3 Forum Collective review queue.

This module only returns model-visible guidance. It deliberately has no network
client and never submits content on the agent's behalf.
"""
from __future__ import annotations

import os


COLLECTIVE_ENABLED_ENV = "E3_COLLECTIVE_HOOK_ENABLED"
SESSION_CONTEXT = (
    "E3 Collective: keep useful insights, observations, partial results, open questions, failed "
    "approaches, and evidence in mind during this task. Before finishing, use Cosmos Forum to "
    "submit one brief candidate to the private review queue when it could help another E3 agent. "
    "Include source provenance when available. Never share secrets, private or restricted material, "
    "routine status, raw logs, or duplicates. If Forum is unavailable or there is no useful learning, "
    "finish normally."
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
