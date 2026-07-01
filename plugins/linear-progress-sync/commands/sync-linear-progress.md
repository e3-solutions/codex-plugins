# /sync-linear-progress

Process queued Linear progress sync events in the foreground with visible Linear MCP calls.

## Arguments

- `root`: target repository root. If omitted, use the current working repository.
- `limit`: maximum eligible events to process. Default: `5`.

## Workflow

1. Run the foreground prepare command:

   ```bash
   python3 /Users/nitishjoshi/Projects/codex-plugins/plugins/linear-progress-sync/scripts/foreground_sync.py prepare --root <root> --limit <limit>
   ```

2. For each `eligible` event returned:
   - Use `mcp__linear.get_issue` for `issue_key`.
   - If the issue is terminal (`Done`, `Completed`, `Closed`, `Canceled`, or equivalent), do not modify Linear. Run the event's `skip_command` with a terminal-state reason.
   - Use `mcp__linear.list_comments` and check whether the exact commit SHA, short SHA, or event ID is already present.
   - If a matching comment already exists, run the event's `ack_command` and do not add a duplicate comment.
   - Otherwise call `mcp__linear.save_comment` with the provided `comment_body`.
   - Read comments back with `mcp__linear.list_comments`.
   - Run the event's `ack_command` only after the new comment is visible.

3. If Linear write approval is denied, MCP tools are unavailable, or read-back does not show the comment, leave the event queued and explain the failure.

## Safety Rules

- Never mark an issue Done, Completed, Closed, Canceled, or terminal.
- Only add comments; do not create issues.
- Do not acknowledge a queued event until the Linear comment is confirmed visible.
- Do not process low-confidence events; leave them queued or in local review.
