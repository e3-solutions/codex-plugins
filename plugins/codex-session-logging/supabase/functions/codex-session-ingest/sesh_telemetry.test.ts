// COR-4888: Sesh plugin labels (arm, version, timing, guard, plan cue) behind a flag, off by default.
import { sanitizeEventPayload, sessionLevelMetadata } from "./event_sanitizer.ts";

type JsonObject = Record<string, unknown>;

function assertEquals(actual: unknown, expected: unknown): void {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`Expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

function restoreEnv(name: string, previous: string | undefined): void {
  if (previous === undefined) {
    Deno.env.delete(name);
    return;
  }
  Deno.env.set(name, previous);
}

Deno.test("sanitizeEventPayload keeps Sesh plugin labels only behind CODEX_SESSION_INGEST_SESH_TELEMETRY", () => {
  const previous = Deno.env.get("CODEX_SESSION_INGEST_SESH_TELEMETRY");
  const labels = {
    sesh_arm: "treated",
    sesh_routine_arm: "need_only",
    sesh_holdout_pct: 15,
    sesh_plugin_version: "0.1.0",
  };
  const event = (event_type: string, metadata: JsonObject, eventMetadata: JsonObject = metadata) =>
    sanitizeEventPayload(
      {
        id: "7c1f0a52-2222-4333-8444-955566667777",
        session_id: "6f0e2a1c-0000-4000-8000-000000000001",
        seq: 1500000001,
        event_type,
        created_at: "2026-10-19T00:00:00.000Z",
        metadata,
      },
      { metadata: eventMetadata },
    ).metadata as JsonObject;
  const search = "mcp__cf0ec861-5a4e-415b-b73e-6e017f49ace3__sesh__search_coding_sessions";
  const started = { tool_name: search, tool_phase: "started", ...labels, duration_ms: 812,
    sesh_subagent: true, sesh_turn_guard: "denied" };
  const plan = { ...labels, sesh_plan_cue: "shown", sesh_plan_kind: "service",
    sesh_plan_topic_sha256: "a".repeat(64), sesh_plan_cue_index: 1 };
  try {
    // Off (the default): exactly today's rows.
    Deno.env.delete("CODEX_SESSION_INGEST_SESH_TELEMETRY");
    assertEquals(event("tool_call_started", started), { tool_name: search, tool_phase: "started" });
    assertEquals(event("environment_snapshot", { source: "startup", ...labels }), { source: "startup" });
    assertEquals(event("sesh_plan_cue_shown", { platform: "claude-code", ...plan }), { platform: "claude-code" });

    Deno.env.set("CODEX_SESSION_INGEST_SESH_TELEMETRY", "1");
    assertEquals(event("tool_call_started", started), started);
    assertEquals(event("environment_snapshot", { source: "startup", ...labels }), { source: "startup", ...labels });
    assertEquals(event("sesh_plan_cue_withheld", { ...plan, sesh_plan_cue: "withheld" }),
      { ...plan, sesh_plan_cue: "withheld" });
    // Other tools, bad values and disagreeing copies are dropped.
    assertEquals(event("tool_call_finished", { tool_name: "Bash", tool_phase: "finished", ...labels }),
      { tool_name: "Bash", tool_phase: "finished" });
    assertEquals(
      event("tool_call_finished", { tool_name: search, tool_phase: "finished", sesh_arm: "question text",
        sesh_holdout_pct: 15.5, sesh_plugin_version: "0.1.0; drop", duration_ms: -1, sesh_turn_guard: "denied" }),
      { tool_name: search, tool_phase: "finished" },
    );
    const { sesh_arm: _disagrees, ...agreeing } = started;
    assertEquals(event("tool_call_started", started, { ...started, sesh_arm: "holdout" }), agreeing);
    assertEquals(event("tool_permission_requested", { tool_name: search, ...labels }), { tool_name: search });
    assertEquals(event("sesh_plan_cue_shown", { ...plan, sesh_plan_kind: "anything", sesh_plan_cue_index: 3 }),
      { ...labels, sesh_plan_cue: "shown", sesh_plan_topic_sha256: "a".repeat(64) });
  } finally {
    restoreEnv("CODEX_SESSION_INGEST_SESH_TELEMETRY", previous);
  }
});

Deno.test("per-event Sesh labels never stick to the session row", () => {
  const event = { platform: "claude-code", sesh_arm: "treated", sesh_plugin_version: "0.1.0",
    duration_ms: 5, sesh_subagent: true, sesh_turn_guard: "denied", sesh_plan_cue: "shown",
    sesh_plan_kind: "module", sesh_plan_topic_sha256: "a".repeat(64), sesh_plan_cue_index: 1 };
  assertEquals(sessionLevelMetadata(event),
    { platform: "claude-code", sesh_arm: "treated", sesh_plugin_version: "0.1.0" });
  // Old clients' metadata passes through unchanged.
  const old = { cwd: "/r", platform: "codex", tool_name: "x", sesh_request_id: "y" };
  assertEquals(sessionLevelMetadata(old), old);
});
