import { assertEquals, assertMatch } from "jsr:@std/assert@1.0.19";
import { handleRequest, type IngestDependencies } from "./index.ts";

const WORKSPACE_ID = "11111111-1111-4111-8111-111111111111";
const INSTALLATION_ID = "22222222-2222-4222-8222-222222222222";
const TOKEN = "test-ingest-token";

Deno.test("rejects a missing ingest token", async () => {
  const response = await handleRequest(
    requestFor(await envelope()),
    dependencies([]),
  );
  assertEquals(response.status, 401);
  assertEquals(await response.json(), { error: "unauthorized" });
});

Deno.test("uploads immutable bytes before committing the complete projection", async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const response = await handleRequest(
    requestFor(await envelope(), TOKEN),
    dependencies(calls),
  );

  assertEquals(response.status, 200);
  assertEquals(calls.length, 2);
  assertMatch(
    calls[0].url,
    /storage\/v1\/object\/agent-rollouts\/workspaces\//,
  );
  assertMatch(calls[1].url, /rest\/v1\/rpc\/commit_agent_batch_v1$/);

  const rpcBody = JSON.parse(String(calls[1].init?.body));
  assertEquals(rpcBody.p_workspace_id, WORKSPACE_ID);
  assertEquals(
    rpcBody.p_records[0].payload.content,
    "Ship the complete history",
  );
  assertEquals(rpcBody.p_raw.byte_length, 43);
});

Deno.test("treats an existing content-addressed raw object as retry-safe", async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const response = await handleRequest(
    requestFor(await envelope(), TOKEN),
    dependencies(calls, true),
  );
  assertEquals(response.status, 200);
  assertEquals(calls.length, 2);
});

Deno.test("does not write when the raw hash is wrong", async () => {
  const calls: Array<{ url: string; init?: RequestInit }> = [];
  const body = await envelope();
  (body.raw as Record<string, unknown>).sha256 = "0".repeat(64);
  const response = await handleRequest(
    requestFor(body, TOKEN),
    dependencies(calls),
  );
  assertEquals(response.status, 400);
  assertEquals(calls.length, 0);
});

function dependencies(
  calls: Array<{ url: string; init?: RequestInit }>,
  duplicateStorage = false,
): IngestDependencies {
  const values: Record<string, string> = {
    CODEX_AGENT_INGEST_TOKEN: TOKEN,
    CODEX_AGENT_WORKSPACE_ID: WORKSPACE_ID,
    SUPABASE_URL: "https://example.supabase.co",
    SUPABASE_SERVICE_ROLE_KEY: "service-role-key",
  };
  return {
    env: (name) => values[name],
    fetch: (input, init) => {
      const url = String(input);
      calls.push({ url, init });
      if (url.includes("/storage/v1/object/")) {
        return Promise.resolve(
          duplicateStorage
            ? new Response('{"message":"The resource already exists"}', {
              status: 409,
            })
            : new Response("{}", { status: 200 }),
        );
      }
      return Promise.resolve(
        new Response(
          JSON.stringify({
            ok: true,
            accepted_records: 1,
            inserted_records: 1,
          }),
          { status: 200 },
        ),
      );
    },
  };
}

function requestFor(body: unknown, token?: string): Request {
  const headers: Record<string, string> = {
    "content-type": "application/json",
  };
  if (token) headers["x-codex-agent-ingest-token"] = token;
  return new Request("https://example.test/agent-ingest-v1", {
    method: "POST",
    headers,
    body: JSON.stringify(body),
  });
}

async function envelope(): Promise<Record<string, unknown>> {
  const rawText = '{"type":"message","content":"hello world"}\n';
  const rawBytes = new TextEncoder().encode(rawText);
  const hash = new Uint8Array(await crypto.subtle.digest("SHA-256", rawBytes));
  const sha256 = [...hash].map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
  let binary = "";
  for (const byte of rawBytes) binary += String.fromCharCode(byte);

  return {
    schema_version: 1,
    installation: {
      id: INSTALLATION_ID,
      actor_key: "arya@example.test",
      client_name: "codex",
      first_seen_at: "2026-08-14T17:00:00Z",
    },
    run: {
      id: "33333333-3333-4333-8333-333333333333",
      conversation_id: "33333333-3333-4333-8333-333333333333",
      root_run_id: "33333333-3333-4333-8333-333333333333",
      agent_role: "root",
      started_at: "2026-08-14T17:00:00Z",
      last_seen_at: "2026-08-14T17:00:01Z",
      origin: "codex://unscoped",
    },
    records: [{
      source: "rollout",
      record_key: "message-1",
      source_sequence: 1,
      record_kind: "message",
      role: "user",
      content_text: "Ship the complete history",
      occurred_at: "2026-08-14T17:00:01Z",
      raw_byte_offset: 0,
      raw_byte_length: rawBytes.byteLength,
      payload: { role: "user", content: "Ship the complete history" },
    }],
    raw: {
      encoding: "identity",
      sha256,
      data_base64: btoa(binary),
    },
  };
}
