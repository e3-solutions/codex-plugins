#!/usr/bin/env python3
from __future__ import annotations

from linear_sync import ensure_state, read_state


def main() -> None:
    try:
        from update_plugin import maybe_spawn_auto_update

        maybe_spawn_auto_update()
    except Exception:
        pass
    ensure_state()
    state = read_state()
    cache = state.get("stale_issue_cache") or {}
    issues = cache.get("issues") or []
    if not issues:
        return
    print("Linear Progress Sync cached issue context:")
    for issue in issues[:10]:
        if not isinstance(issue, dict):
            continue
        key = issue.get("identifier") or issue.get("id") or issue.get("key")
        title = issue.get("title")
        status = issue.get("status") or issue.get("state")
        print(f"- {key}: {title} [{status}]")


if __name__ == "__main__":
    main()
