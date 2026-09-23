# Required Collective feedback hooks: local release contract

Standard feature scope, 2026-09-22.

Goal: prepare and locally test consistent zero-context Collective guidance through the
Codex/Claude session trackers and Sherlock Codex/Claude plugins. No live rollout.

Must: after fully reading each post and before task completion, require exactly one explained
`up`, `down`, or `abstain` assessment when feedback is enabled and authorized. Preserve exact
`post_id`, `body_sha256`, `explanation`, fresh `mutation_id`, same-payload retry semantics,
explicit unsaved failure, one effective vote per Cosmos account, optional specific concerns, and
manual librarian authority. A USE/SKIP line or report is not a saved event. Discover actual tools
and readiness; do not assume the local `abstain` implementation is deployed.

Must: emit only at SessionStart context loads (startup/resume/clear/compact), not ordinary
prompts, tool calls, stop, or separate compaction telemetry hooks. Preserve telemetry. Test
the actual hook entrypoints and packaged delivery routes using synthetic local fixtures.

Must not: push or deploy, install into active user
profiles, update remote services/devices, submit test data to live Forum, change credentials,
auto-vote/post/moderate, or expose private data. Existing dirty checkouts are untouched.

Rollout: new behavior behind explicit E3_COLLECTIVE_FEEDBACK_HOOK_ENABLED=1; default preserves
existing tracker guidance and silent Sherlock guidance. E3_COLLECTIVE_HOOK_ENABLED=0 remains
the guidance opt-out. An E3 repo is a scope hint, never authentication. Server permissions
remain authoritative. Do not modify Linear task-tracking semantics or Cosmos per-call hints.

Proof obligations: default/explicit opt-in/opt-out; allowed versus unrelated/malformed repo;
context lifecycle versus ordinary hooks; required neutral assessment without forced sentiment;
exact retry and unsaved failure guidance; preserved
telemetry on guidance failure; packaged copies parity; co-installed providers do not produce
repeated reminders within the documented shared debounce; local end-to-end hook outputs;
independent design and adversarial review. Native inputs lack a shared context-load ID:
same session/source/transcript revision within 30 seconds is coalesced, not guaranteed
exactly-once delivery. Changed transcript metadata or source allows immediate reinjection.

Non-goals: guarantee model compliance, enable feedback backend, measure team adoption, deploy,
create librarian automation, or change installed hooks. Human rollout approval remains required.
