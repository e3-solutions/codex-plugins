#!/usr/bin/env python3
"""Bounded agent guidance for the E3 Forum Collective review queue.

This module only returns model-visible guidance. It deliberately has no network
client and never submits content on the agent's behalf.
"""
from __future__ import annotations

import os


COLLECTIVE_ENABLED_ENV = "E3_COLLECTIVE_HOOK_ENABLED"
SESSION_CONTEXT = (
    "E3 Collective: use Cosmos Forum to learn from relevant prior E3 reasoning, decisions, "
    "investigations, failures, and methods. Query authoritative systems for current state; do not "
    "use or submit Forum as a cache of directly queryable facts such as access, health, PR, "
    "deployment, or configuration status. Before finishing, if this work produced a reusable "
    "non-queryable insight or materially changed prior Collective knowledge, search published Forum "
    "work using the repository, ticket, service, PR, and topic. Submit one concise candidate to the "
    "private review queue, link relevant work as supports, challenges, or supersedes, and include an "
    "observation time and source provenance when needed. Never share secrets, private or restricted "
    "material, routine status, raw logs, or duplicates. If Forum is unavailable or there is no useful "
    "learning, finish normally."
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
