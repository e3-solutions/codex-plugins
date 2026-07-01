# /linear-start

Start Linear-linked implementation work before any code edits.

## Arguments

- `issue`: existing Linear issue identifier, for example `COR-123`.
- `team`: Linear team name or ID, required only when creating a new issue and no default is known.
- `title`: Linear issue title, required when creating a new issue.
- `root`: target repository root. If omitted, use the current working repository.
- `dry-run`: print the local GitHub kickoff plan without changing GitHub or local active state.

## Workflow

1. Resolve the Linear issue.
   - If `issue` is present, call `mcp__linear.get_issue` for that issue.
   - Otherwise call `mcp__linear.save_issue` with `team`, `title`, and `assignee: "me"` when appropriate, then call `mcp__linear.get_issue` for the created issue.
   - If no `team` is available, call `mcp__linear.list_teams` and ask the user to choose once.

2. Choose the branch name.
   - Use the Linear issue's returned git branch name when present.
   - Otherwise use `arya/<ISSUE-KEY>-<title-slug>`.

3. Run the local kickoff helper:

   ```bash
   python3 <plugin-root>/scripts/linear_start.py kickoff \
     --root <root> \
     --issue-key <ISSUE-KEY> \
     --issue-title "<Linear issue title>" \
     --issue-url "<Linear issue URL>" \
     --branch "<Linear branch name>"
   ```

4. Link Linear back to GitHub.
   - Read the helper JSON output.
   - Call `mcp__linear.save_issue` with `id: <ISSUE-KEY>` and a PR link attachment when `pr_url` is present.
   - Call `mcp__linear.save_comment` on the issue with the branch, draft PR URL, and kickoff commit summary.
   - Move to `In Progress` only if the state exists and is non-terminal.

## Safety Rules

- Do not write code before this workflow succeeds.
- Use Linear MCP/app tools only; do not use a direct Linear API client.
- Use `Refs <ISSUE-KEY>` in PR body text, not `Fixes`, so the kickoff does not imply closure.
- Leave terminal Linear issues unchanged.
