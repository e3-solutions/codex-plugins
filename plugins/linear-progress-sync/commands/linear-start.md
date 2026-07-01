# /linear-start

Start Linear-linked implementation work before any code edits.

## Arguments

- `issue`: existing Linear issue identifier, for example `COR-123`.
- `team`: Linear team name or ID. Overrides the saved repo binding for this kickoff.
- `project`: Linear project name or ID. Overrides the saved repo binding for this kickoff.
- `title`: Linear issue title, required when creating a new issue.
- `root`: target repository root. If omitted, use the current working repository.
- `dry-run`: print the local GitHub kickoff plan without changing GitHub or local active state.
- `configure`: update the saved Linear team/project binding for this repo.

## Workflow

1. Resolve this repo's Linear destination.
   - Run `python3 <plugin-root>/scripts/linear_start.py repo-binding --root <root>`.
   - If no binding exists and this command is not using an existing `issue`, list Linear teams/projects with the connected Linear app tools and ask the user once which team/project this repo should use.
   - Save the answer with:

     ```bash
     python3 <plugin-root>/scripts/linear_start.py configure-repo \
       --root <root> \
       --team "<Linear team>" \
       --project "<Linear project>"
     ```

   - Future kickoff in this repo uses the saved binding automatically.

2. Resolve the Linear issue.
   - If `issue` is present, read it with the connected Linear app issue lookup tool.
   - Otherwise create the issue with the connected Linear app issue save/create tool using `team`, `project`, `title`, and `assignee: "me"` when appropriate.
   - Read the issue back after create/update so the branch name, URL, title, team, and project are confirmed.

3. Choose the branch name.
   - Use the Linear issue's returned git branch name when present.
   - Otherwise use `arya/<ISSUE-KEY>-<title-slug>`.

4. Run the local kickoff helper:

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

5. Link Linear back to GitHub.
   - Read the helper JSON output.
   - Use the connected Linear app issue save/update tool to attach the PR link when `pr_url` is present.
   - Use the connected Linear app comment tool to add the branch, draft PR URL, and kickoff commit summary.
   - Move to `In Progress` only if the state exists and is non-terminal.

## Safety Rules

- Do not write code before this workflow succeeds.
- Use Linear MCP/app tools only; do not use a direct Linear API client.
- Use `Refs <ISSUE-KEY>` in PR body text, not `Fixes`, so the kickoff does not imply closure.
- Leave terminal Linear issues unchanged.
