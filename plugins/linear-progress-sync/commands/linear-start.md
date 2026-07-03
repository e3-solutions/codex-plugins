# /linear-start

Start Linear-linked implementation work before any code edits.

## Arguments

- `issue`: existing Linear issue identifier, for example `COR-123`.
- `team`: Linear team name or ID. Overrides the saved repo binding for this kickoff.
- `project`: Linear project name or ID. Overrides the saved repo binding for this kickoff.
- `title`: Linear issue title, required when creating a new issue.
- `root`: target repository root. If omitted, use the current working repository.
- `dry-run`: print the local GitHub kickoff plan without changing GitHub or active state.
- `configure`: update the saved Linear team/project binding for this repo.

## Workflow

1. Resolve the global Linear user profile.
   - Run `python3 <plugin-root>/scripts/linear_start.py user-profile --root <root>`.
   - If no profile exists, ask the user once what their name on Linear is.
   - Save the answer with:

     ```bash
     python3 <plugin-root>/scripts/linear_start.py configure-user \
       --linear-name "<Linear user name>"
     ```

   - This is a user-level setting stored in `~/.codex/linear-sync/user.json` and reused for every repo.
   - Do not create the Linear issue, branch, PR, or code changes until the user profile is saved.

2. Resolve this repo's Linear destination.
   - Run `python3 <plugin-root>/scripts/linear_start.py repo-binding --root <root>`.
   - If no binding exists, call `mcp__codex_apps__linear._list_teams` and `mcp__codex_apps__linear._list_projects`, then ask the user once which team/project this repo should use.
   - If the direct Linear MCP namespace is exposed instead, use `mcp__linear.list_teams` and `mcp__linear.list_projects`.
   - If the session exposes short Linear aliases like `list_teams` or `list_projects`, use those aliases. If create/update tools are not visible after listing, search/load Linear tools; do not stop after listing projects.
   - Save the answer with:

     ```bash
     python3 <plugin-root>/scripts/linear_start.py configure-repo \
       --root <root> \
       --team "<Linear team>" \
       --project "<Linear project>"
   ```

   - Future kickoff in this repo uses the saved binding automatically.
   - Do not create the Linear issue, branch, PR, or code changes until the chosen repo destination is saved.
   - If a write is blocked because no repo destination is saved, do not answer with a code patch or say you are blocked. Continue by listing Linear destinations and asking the user which team/project this repo should use.

3. Resolve the Linear issue.
   - If `issue` is present, read it with `mcp__codex_apps__linear._get_issue` or `mcp__codex_apps__linear._fetch` using the known issue identifier. If the direct Linear MCP namespace is exposed instead, use `mcp__linear.get_issue`.
   - Otherwise create a new issue automatically from the user's implementation request with `mcp__codex_apps__linear._save_issue` using `team`, `project`, `title`, and `assignee: <stored Linear user name>` when appropriate. Do not ask the user for a Linear issue key.
   - End any Linear issue body or kickoff comment Codex creates with `Codex bot: <stored Linear user name> at <ISO-8601 UTC timestamp>`.
   - Read the issue back with `mcp__codex_apps__linear._get_issue`/`mcp__codex_apps__linear._fetch` or `mcp__linear.get_issue` after create/update so the branch name, URL, title, team, and project are confirmed.

4. Choose the branch name.
   - Use the Linear issue's returned git branch name when present.
   - Otherwise use `arya/<ISSUE-KEY>-<title-slug>`.

5. Run the local kickoff helper. This creates the branch, empty kickoff commit, and draft PR, then prints `pending_active_state`; it does not write `active.json` yet.
   - If a write is blocked after the Linear issue was created, do not stop, do not test write access, and do not ask the user to activate state. Reuse the issue key, URL, title, and Linear `gitBranchName`; run this helper; link Linear and GitHub; then run the helper's `activation_command`.

   ```bash
   python3 <plugin-root>/scripts/linear_start.py kickoff \
     --root <root> \
     --issue-key <ISSUE-KEY> \
     --issue-title "<Linear issue title>" \
     --issue-url "<Linear issue URL>" \
     --branch "<Linear branch name>" \
     --team "<Linear team>" \
     --project "<Linear project>"
   ```

6. Link Linear back to GitHub.
   - Read the helper JSON output.
   - Use `mcp__codex_apps__linear._save_issue` or `mcp__linear.save_issue` to attach the required PR link from `pr_url`.
   - Use `mcp__codex_apps__linear._save_comment` or `mcp__linear.save_comment` to add the branch, draft PR URL, kickoff commit summary, and attribution footer.
   - Move to `In Progress` only if the state exists and is non-terminal.
   - Read Linear back with `mcp__codex_apps__linear._fetch` or confirm the saved comment/link is visible before continuing.

7. Activate local state only after Linear link/comment confirmation.
   - Run the `activation_command` from the helper output, or run:

     ```bash
     python3 <plugin-root>/scripts/linear_start.py activate \
       --root <root> \
       --issue-key <ISSUE-KEY> \
       --issue-title "<Linear issue title>" \
       --issue-url "<Linear issue URL>" \
       --branch "<Linear branch name>" \
       --pr-url "<GitHub draft PR URL>" \
       --pr-number <GitHub PR number> \
       --team "<Linear team>" \
       --project "<Linear project>"
     ```

## Safety Rules

- Do not write code before activation succeeds.
- Use Linear MCP/app tools only; do not use a direct Linear API client.
- Use `Refs <ISSUE-KEY>` in PR body text, not `Fixes`, so the kickoff does not imply closure.
- Leave terminal Linear issues unchanged.
