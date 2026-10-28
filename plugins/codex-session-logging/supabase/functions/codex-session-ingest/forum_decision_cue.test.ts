// COR-5027: the COR-4911 Forum decision-point cue keeps its content-free labels at ingest.
import { sanitizeEventPayload, sessionLevelMetadata } from "./event_sanitizer.ts";

type JsonObject = Record<string, unknown>;

function assertEquals(actual: unknown, expected: unknown): void {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`Expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

const event = (event_type: string, metadata: JsonObject, eventMetadata: JsonObject = metadata) =>
  sanitizeEventPayload(
    {
      id: "7c1f0a52-2222-4333-8444-955566667779",
      session_id: "019a2b3c-0000-7000-8000-000000000003",
      seq: 41,
      event_type,
      hook_event_name: "PreToolUse",
      created_at: "2026-10-28T00:00:00.000Z",
      metadata,
    },
    { metadata: eventMetadata },
  ).metadata as JsonObject;

// Key order is the sanitizer's output order (assertEquals compares JSON text).
const shown = {
  cwd: "/repo",
  forum_decision_cue: "shown",
  forum_decision_cue_reason: "first_edit",
  forum_decision_cue_index: 1,
  forum_searches_in_turn_before: 0,
  forum_decision_cue_version: "2026-10-21",
};

Deno.test("Forum decision cue labels are kept for both arms, with or without the Sesh flag", () => {
  const previous = Deno.env.get("CODEX_SESSION_INGEST_SESH_TELEMETRY");
  try {
    for (const flag of [undefined, "1"]) {
      if (flag === undefined) {
        Deno.env.delete("CODEX_SESSION_INGEST_SESH_TELEMETRY");
      } else {
        Deno.env.set("CODEX_SESSION_INGEST_SESH_TELEMETRY", flag);
      }
      assertEquals(event("forum_decision_cue_shown", shown), shown);
      const withheld = { ...shown, forum_decision_cue: "withheld", forum_decision_cue_reason: "call_6",
        forum_decision_cue_index: 3, forum_searches_in_turn_before: 2 };
      assertEquals(event("forum_decision_cue_withheld", withheld), withheld);
      // Claude Code adds platform/agent/permission_mode; those still pass as before.
      const { cwd: _cwd, ...labels } = shown;
      assertEquals(event("forum_decision_cue_shown", { ...shown, platform: "claude-code", agent: "claude" }),
        { cwd: "/repo", platform: "claude-code", agent: "claude", ...labels });
    }
  } finally {
    if (previous === undefined) {
      Deno.env.delete("CODEX_SESSION_INGEST_SESH_TELEMETRY");
    } else {
      Deno.env.set("CODEX_SESSION_INGEST_SESH_TELEMETRY", previous);
    }
  }
});

Deno.test("Forum decision cue: bad values, a mismatched arm, extra keys and disagreeing copies are dropped", () => {
  assertEquals(
    event("forum_decision_cue_shown", {
      forum_decision_cue: "withheld", // must match the event type
      forum_decision_cue_reason: "the user asked about OCR",
      forum_decision_cue_index: 1.5,
      forum_searches_in_turn_before: -1,
      forum_decision_cue_version: "2026-10-21; drop",
      prompt: "PRIVATE_PROMPT_SENTINEL",
      query: "PRIVATE_QUERY_SENTINEL",
      forum_decision_cue_text: "anything",
    }),
    {},
  );
  assertEquals(event("forum_decision_cue_shown", { forum_decision_cue_reason: "call_51",
    forum_decision_cue_index: 11, forum_searches_in_turn_before: 1001, forum_decision_cue_version: "2026-13-01" }),
    {});
  assertEquals(event("forum_decision_cue_shown", { forum_decision_cue_reason: "call_7",
    forum_decision_cue_index: 10, forum_searches_in_turn_before: 1000 }),
    { forum_decision_cue_reason: "call_7", forum_decision_cue_index: 10, forum_searches_in_turn_before: 1000 });
  // The stored event copy disagrees on one key: only that key is dropped.
  const { forum_decision_cue_index: _index, ...agreeing } = shown;
  assertEquals(event("forum_decision_cue_shown", shown, { ...shown, forum_decision_cue_index: 2 }), agreeing);
  // Only the two cue event types carry these keys.
  assertEquals(event("thread_prompt_submitted", { ...shown }), { cwd: "/repo" });
  assertEquals(event("tool_call_started", { tool_name: "Edit", tool_phase: "started", ...shown }),
    { cwd: "/repo", tool_name: "Edit", tool_phase: "started" });
});

Deno.test("Forum decision cue labels never stick to the session row", () => {
  assertEquals(sessionLevelMetadata({ ...shown, platform: "codex" }), { cwd: "/repo", platform: "codex" });
});
