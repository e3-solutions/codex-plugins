import { sanitizeEventPayload } from "./event_sanitizer.ts";

function assertEquals(actual: unknown, expected: unknown): void {
  if (JSON.stringify(actual) !== JSON.stringify(expected)) {
    throw new Error(`Expected ${JSON.stringify(expected)}, got ${JSON.stringify(actual)}`);
  }
}

for (const toolName of ["mcp__e3__sesh__search_coding_sessions",
  "mcp__e3_cosmos__sesh__search_coding_sessions",
  "mcp__cosmos__sesh__search_coding_sessions",
  "mcp__cosmos_e3__sesh__search_coding_sessions"]) {
Deno.test(`origin linkage is bounded client-attested finished-search metadata: ${toolName}`, () => {
  const context = "11111111-1111-4111-8111-111111111111";
  const origin = "22222222-2222-4222-8222-222222222222";
  const record = { id: context, session_id: context, seq: 1,
    event_type: "tool_call_finished", created_at: "2026-09-18T00:00:00Z",
    metadata: { tool_name: toolName,
      tool_phase: "finished", sesh_request_id: context,
      sesh_origin_session_id: origin, sesh_context_session_id: context,
      sesh_origin_basis: "client_transcript_header_v1" } };
  const clean = sanitizeEventPayload(record, {});
  assertEquals(clean.metadata, record.metadata);
  const conflicting = sanitizeEventPayload(record, { metadata: {
    ...record.metadata, sesh_origin_session_id: context,
  } });
  assertEquals((conflicting.metadata as Record<string, unknown>).sesh_origin_session_id, undefined);
  const wrongRequest = sanitizeEventPayload(record, { metadata: {
    ...record.metadata, sesh_request_id: origin,
  } });
  assertEquals((wrongRequest.metadata as Record<string, unknown>).sesh_origin_session_id, undefined);
  const originOnly = { sesh_origin_session_id: origin, sesh_context_session_id: context,
    sesh_origin_basis: "client_transcript_header_v1" };
  const borrowedRequest = sanitizeEventPayload({ ...record, metadata: originOnly },
    { metadata: record.metadata });
  assertEquals((borrowedRequest.metadata as Record<string, unknown>).sesh_origin_session_id, undefined);
  const incomplete = { ...record.metadata } as Record<string, unknown>;
  delete incomplete.sesh_origin_basis;
  const spliced = sanitizeEventPayload({ ...record, metadata: incomplete },
    { metadata: { sesh_origin_basis: "client_transcript_header_v1" } });
  assertEquals((spliced.metadata as Record<string, unknown>).sesh_origin_session_id, undefined);
  for (const patch of [
    { sesh_origin_session_id: "secret text" },
    { sesh_context_session_id: origin },
    { sesh_origin_basis: "server_verified" },
    { sesh_request_id: "invalid" },
    { tool_name: "unrelated" },
    { tool_name: `${toolName}_extra` },
  ]) {
    const result = sanitizeEventPayload({ ...record,
      metadata: { ...record.metadata, ...patch } }, {});
    assertEquals((result.metadata as Record<string, unknown>).sesh_origin_session_id, undefined);
  }
  const started = sanitizeEventPayload({ ...record, event_type: "tool_call_started" }, {});
  assertEquals((started.metadata as Record<string, unknown>).sesh_origin_session_id, undefined);
});
}
