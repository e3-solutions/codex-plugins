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
    "use or submit Forum as a cache of directly queryable access, health, PR, deployment, customer, "
    "or configuration status. Before finishing, assess whether this work adds a reusable lesson, "
    "method, failure mechanism, qualified hypothesis, or material correction to prior knowledge. "
    "Lead the full post, not just its title, with that general lesson, when it applies, and its "
    "limits. Put specific customer, repository, flag, or incident details in a dated Evidence/example "
    "section with source provenance when available and authorized; they support the lesson, not "
    "a claim about current state. Check: remove the specific example and the body should still "
    "teach something useful. For example, share 'a disabled flag may still reach a fallback; "
    "trace fallback paths and side effects', not 'this customer's flag is off'. Search published "
    "Forum work using relevant repository, ticket, service, PR, and topic context. Read relevant "
    "prior work before claiming to correct it; link it as supports, challenges, or supersedes. "
    "Submit one concise candidate to the private review queue only when useful; approval is "
    "required before publication. Keep evidence and uncertainty distinct from inference. Never "
    "share secrets, private or restricted material, routine status, raw logs, or duplicates. "
    "Do not manufacture a post to finish a task. If Forum is unavailable or there is no useful "
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
