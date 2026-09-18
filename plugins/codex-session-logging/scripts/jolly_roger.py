#!/usr/bin/env python3
"""Managed protocol adapter; native standalone hooks remain unchanged.

Successful handling means local capture/guidance completed. Existing detached
queue upload is best effort and is never reported as remote delivery.
"""
from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
import sys

# Central invocation uses -I -B: explicitly import only bundled siblings.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True
os.environ["PYTHONDONTWRITEBYTECODE"] = "1"

ENTRYPOINTS = {
    "SessionStart": "session_start",
    "UserPromptSubmit": "user_prompt_submit",
    "PreToolUse": "pre_tool_use",
    "PostToolUse": "post_tool_use",
    "Stop": "stop",
}
STATE_ENV = {
    "logger-state": "CODEX_SESSION_LOG_STATE_DIR",
    "logger-preferences": "CODEX_SESSION_LOG_PREFERENCES_DIR",
    "forum-feedback": "E3_COLLECTIVE_HOOK_STATE_DIR",
}


def prepare(envelope: object, mode: str) -> tuple[str, dict]:
    if not isinstance(envelope, dict) or set(envelope) != {
        "schema_version", "provider", "event", "payload"
    }:
        raise ValueError("invalid envelope")
    if type(envelope["schema_version"]) is not int or envelope["schema_version"] != 1:
        raise ValueError("unsupported schema")
    providers = {"codex", "claude"} if mode == "guidance" else {"codex"}
    events = {"SessionStart"} if mode == "guidance" else set(ENTRYPOINTS)
    if envelope["provider"] not in providers or envelope["event"] not in events:
        raise ValueError("unsupported provider/event")
    if not isinstance(envelope["payload"], dict):
        raise ValueError("invalid payload")
    if os.environ.get("JOLLY_ROGER_MANAGED") != "1" or os.environ.get(
        "JOLLY_ROGER_PROVIDER"
    ) != envelope["provider"] or os.environ.get("JOLLY_ROGER_COMPONENT_ID") != "codex-session-logging":
        raise ValueError("managed environment required")
    paths = json.loads(os.environ.get("JOLLY_ROGER_STATE_PATHS", "null"))
    if not isinstance(paths, dict) or set(paths) != set(STATE_ENV):
        raise ValueError("explicit legacy state mappings required")
    for value in paths.values():
        if not isinstance(value, str) or not Path(value).is_absolute() or ".." in Path(value).parts:
            raise ValueError("absolute state mappings required")
    # Validate the entire mapping before setting any override. No migration,
    # directory copying, identity changes, or provider-home substitution.
    for state_id, variable in STATE_ENV.items():
        os.environ[variable] = paths[state_id]
    return envelope["event"], envelope["payload"]


def guidance(payload: dict) -> None:
    from sesh_context import sesh_context
    from collective import session_context
    from collective_feedback import feedback_context
    from session_logging import should_prompt_collective

    context = sesh_context(payload)
    if context:
        print(context)
    if os.environ.get("E3_COLLECTIVE_FEEDBACK_HOOK_ENABLED", "1") == "1":
        context = feedback_context(
            payload, event_name="SessionStart", agent=os.environ["JOLLY_ROGER_PROVIDER"]
        )
    else:
        context = session_context(eligible=should_prompt_collective(payload))
    if context:
        print(context)


def capture(event: str, payload: dict) -> None:
    from session_logging import capture_hook_event
    from rollout_sync import sync_after_hook

    capture_hook_event(payload, event_name=event)
    sync_after_hook(payload, event_name=event)


def main(mode: str = "capture") -> int:
    response: dict = {"status": "skipped"}
    try:
        raw = sys.stdin.buffer.read(4 * 1024 * 1024 + 1)
        if len(raw) > 4 * 1024 * 1024:
            raise ValueError("oversized input")
        event, payload = prepare(json.loads(raw.decode("utf-8")), mode)
        output, diagnostics = io.StringIO(), io.StringIO()
        original_stdin = sys.stdin
        try:
            sys.stdin = io.StringIO(json.dumps(payload))
            with contextlib.redirect_stdout(output), contextlib.redirect_stderr(diagnostics):
                if mode == "guidance":
                    guidance(payload)
                else:
                    capture(event, payload)
        finally:
            sys.stdin = original_stdin
        if diagnostics.getvalue():
            # Legacy exceptions may contain payload/config data. Keep only a
            # fixed diagnostic at this boundary; never relay raw stderr.
            print("codex-session-logging: optional hook work failed", file=sys.stderr)
        context = output.getvalue().strip()
        if (context and event != "SessionStart") or len(context) > 6000:
            raise ValueError("invalid context")
        response = {"status": "ok"}
        if context:
            response["context"] = context
    except Exception:
        print("codex-session-logging: managed hook skipped", file=sys.stderr)
    print(json.dumps(response, ensure_ascii=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
