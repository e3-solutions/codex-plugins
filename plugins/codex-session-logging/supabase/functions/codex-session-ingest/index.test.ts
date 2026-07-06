import { sanitizeEventPayload } from "./event_sanitizer.ts";
import { handleRequest } from "./index.ts";

Deno.test("handleRequest returns 400 for invalid ingest payloads", async () => {
  const response = await handleRequest(
    new Request("https://example.test/codex-session-ingest", {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        record: {
          id: "message-1",
          session_id: "session-1",
          seq: 1,
          role: "user",
        },
        client: {},
        message: { content: "hello" },
      }),
    }),
  );
  const body = await response.json();

  assertEquals(response.status, 400);
  assertEquals(body.error, "invalid_payload");
  assertIncludes(body.message, "client.repo_remote");
});

Deno.test("sanitizeEventPayload keeps only allowlisted tool event fields", () => {
  const sanitized = sanitizeEventPayload(
    {
      id: "804fd832-7779-4665-9bec-2f10462c721b",
      session_id: "session-tools",
      turn_id: "turn-1",
      seq: 7,
      event_type: "tool_call_finished",
      hook_event_name: "PostToolUse",
      created_at: "2026-07-06T00:00:00.000Z",
      metadata: {
        cwd: "/repo",
        tool_name: "functions.exec_command",
        tool_phase: "finished",
        success: true,
        tool_input: { cmd: "echo should-not-store" },
        tool_response: "large output should not store",
        arbitrary_secret: "sk-should-not-store",
      },
    },
    {
      id: "ignored-client-id",
      session_id: "ignored-session",
      event_type: "ignored-type",
      metadata: {
        tool_name: "malicious.override",
        tool_input: { cmd: "echo should-not-store" },
        tool_response: "large output should not store",
        arbitrary_secret: "sk-should-not-store",
      },
    },
  );
  const serialized = JSON.stringify(sanitized);

  assertEquals(sanitized, {
    id: "804fd832-7779-4665-9bec-2f10462c721b",
    session_id: "session-tools",
    turn_id: "turn-1",
    seq: 7,
    event_type: "tool_call_finished",
    hook_event_name: "PostToolUse",
    created_at: "2026-07-06T00:00:00.000Z",
    metadata: {
      cwd: "/repo",
      success: true,
      tool_name: "functions.exec_command",
      tool_phase: "finished",
    },
  });
  assertNotIncludes(serialized, "tool_input");
  assertNotIncludes(serialized, "tool_response");
  assertNotIncludes(serialized, "should-not-store");
  assertNotIncludes(serialized, "arbitrary_secret");
});

Deno.test("sanitizeEventPayload strips secret-bearing setup snapshot fields", () => {
  const sanitized = sanitizeEventPayload(
    {
      id: "904fd832-7779-4665-9bec-2f10462c721b",
      session_id: "session-setup",
      seq: 1,
      event_type: "environment_snapshot",
      hook_event_name: "SessionStart",
      created_at: "2026-07-06T00:00:00.000Z",
      metadata: {
        codex_setup: {
          settings: {
            model: "gpt-5.5",
            approval_policy: "never",
          },
          plugins: [
            {
              name: "github@openai-curated",
              enabled: true,
              path: "/secret/path",
            },
          ],
          skills: [
            { name: "supabase", source: "user", body: "sk-should-not-store" },
          ],
          mcp_servers: [
            {
              name: "local-secret",
              transport: "command",
              args: ["--token", "sk-should-not-store"],
              env: { SECRET_TOKEN: "sk-should-not-store" },
            },
          ],
          connections: [
            {
              id: "asdk_app_linear",
              tools: ["linear.save_issue"],
              token: "sk-should-not-store",
            },
          ],
        },
      },
    },
    {},
  );
  const serialized = JSON.stringify(sanitized);

  assertEquals(sanitized.metadata, {
    codex_setup: {
      settings: {
        model: "gpt-5.5",
      },
      plugins: [
        { name: "github@openai-curated", enabled: true },
      ],
      skills: [
        { name: "supabase", source: "user" },
      ],
      mcp_servers: [
        { name: "local-secret", transport: "command" },
      ],
      connections: [
        { id: "asdk_app_linear", tools: ["linear.save_issue"] },
      ],
    },
  });
  assertNotIncludes(serialized, "sk-should-not-store");
  assertNotIncludes(serialized, "SECRET_TOKEN");
  assertNotIncludes(serialized, "approval_policy");
});

function assertEquals(actual: unknown, expected: unknown): void {
  const actualJson = JSON.stringify(actual, null, 2);
  const expectedJson = JSON.stringify(expected, null, 2);
  if (actualJson !== expectedJson) {
    throw new Error(`Expected:\n${expectedJson}\nActual:\n${actualJson}`);
  }
}

function assertNotIncludes(value: string, pattern: string): void {
  if (value.includes(pattern)) {
    throw new Error(`Expected serialized payload not to include ${pattern}`);
  }
}

function assertIncludes(value: unknown, pattern: string): void {
  if (typeof value !== "string" || !value.includes(pattern)) {
    throw new Error(`Expected ${JSON.stringify(value)} to include ${pattern}`);
  }
}
