// COR-5027: open_session_passages calls link back to the search that returned the session.
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
      id: "7c1f0a52-2222-4333-8444-955566667780",
      session_id: "019a2b3c-0000-7000-8000-000000000004",
      seq: 52,
      event_type,
      created_at: "2026-10-28T00:00:00.000Z",
      metadata,
    },
    { metadata: eventMetadata },
  ).metadata as JsonObject;

const digest = "b".repeat(64);
const requestId = "3f2a1b4c-5d6e-4f70-8a91-b2c3d4e5f607";
const linkage = {
  sesh_opened_source_handle_sha256_v1: digest,
  sesh_source_open_witness_v1: "unknown",
  sesh_open_search_request_id: requestId,
  sesh_open_kind: "open",
  sesh_open_offset: 0,
};

Deno.test("open_session_passages keeps the opened-handle digest and search request id under any Sesh namespace", () => {
  for (const tool of [
    "mcp__cosmos__sesh__open_session_passages",
    "mcp__e3__sesh__open_session_passages",
    "mcp__codex_apps__cosmos__sesh__open_session_passages",
    "mcp__coreedge__sesh__open_session_passages",
    "mcp__e3_mcp__sesh__open_session_passages",
    "mcp__cf0ec861-5a4e-415b-b73e-6e017f49ace3__sesh__open_session_passages",
    "mcp__cosmos__open_session_passages",
  ]) {
    const finished = { tool_name: tool, tool_phase: "finished", ...linkage };
    assertEquals(event("tool_call_finished", finished), finished);
  }
});

Deno.test("open_session_passages: feedback calls, failures and bad values", () => {
  const tool = "mcp__cosmos__sesh__open_session_passages";
  const base = { tool_name: tool, tool_phase: "finished" };
  // A "did it help" call does not read passages: kind feedback, no offset.
  assertEquals(event("tool_call_finished", { ...base, sesh_open_search_request_id: requestId,
    sesh_open_kind: "feedback", sesh_open_offset: 0 }),
    { ...base, sesh_open_search_request_id: requestId, sesh_open_kind: "feedback" });
  assertEquals(event("tool_call_finished", { ...base, success: false, ...linkage,
    sesh_source_open_witness_v1: "failed" }),
    { success: false, ...base, ...linkage, sesh_source_open_witness_v1: "failed" });
  // Passage opens are never "verified" exact bytes; ids must be canonical; no content passes.
  assertEquals(event("tool_call_finished", { ...base, ...linkage, sesh_source_open_witness_v1: "verified",
    sesh_open_search_request_id: "3F2A1B4C-5D6E-4F70-8A91-B2C3D4E5F607", sesh_open_kind: "read",
    sesh_open_offset: 1001, query: "PRIVATE_QUERY_SENTINEL", passages: "PRIVATE_PASSAGE_SENTINEL" }),
    base);
  // Started events and other tools never carry it.
  assertEquals(event("tool_call_started", { tool_name: tool, tool_phase: "started", ...linkage }),
    { tool_name: tool, tool_phase: "started" });
  assertEquals(event("tool_call_finished", { tool_name: "mcp__cosmos__forum__open_session_passages_x",
    tool_phase: "finished", ...linkage }),
    { tool_name: "mcp__cosmos__forum__open_session_passages_x", tool_phase: "finished" });
  // Copies must agree.
  const { sesh_open_search_request_id: _id, ...rest } = linkage;
  assertEquals(event("tool_call_finished", { ...base, ...linkage },
    { ...base, ...linkage, sesh_open_search_request_id: "4f2a1b4c-5d6e-4f70-8a91-b2c3d4e5f607" }),
    { ...base, ...rest });
});

Deno.test("open linkage labels never stick to the session row", () => {
  assertEquals(sessionLevelMetadata({ platform: "codex", sesh_open_search_request_id: requestId,
    sesh_open_kind: "open", sesh_open_offset: 2 }), { platform: "codex" });
});
