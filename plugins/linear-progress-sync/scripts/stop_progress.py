#!/usr/bin/env python3
from __future__ import annotations

import json

from linear_sync import (
    enqueue_event,
    first_issue_key,
    read_state,
    read_stdin_json,
    session_progress_payload,
    should_throttle_session_progress,
    spawn_drain,
)


def main() -> None:
    payload = session_progress_payload(read_stdin_json())
    if payload is None:
        return
    branch_key = first_issue_key(str(payload.get("branch") or ""))
    if branch_key and should_throttle_session_progress(read_state(), branch_key):
        return
    event = enqueue_event("session_progress", payload)
    spawn_drain()
    print(json.dumps({"queued": event["id"], "type": event["type"]}))


if __name__ == "__main__":
    main()

