import { sanitizeEventPayload } from "./event_sanitizer.ts";
import { handleRequest } from "./index.ts";

type JsonObject = Record<string, unknown>;

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

Deno.test("handleRequest preserves existing session codex setup on later event upserts", async () => {
  const requests: Array<{ url: string; body: JsonObject | null }> = [];
  const originalFetch = globalThis.fetch;
  const previousUrl = Deno.env.get("SUPABASE_URL");
  const previousServiceRole = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  const existingSetup = {
    settings: { model: "gpt-5.5" },
    plugins: [{ name: "codex-session-logging@coreedge-local", enabled: true }],
  };

  Deno.env.set("SUPABASE_URL", "https://project.supabase.co");
  Deno.env.set("SUPABASE_SERVICE_ROLE_KEY", "service-role-key");
  globalThis.fetch = async (input, init = {}) => {
    const url = input instanceof Request
      ? input.url
      : input instanceof URL
      ? input.toString()
      : input;
    const requestInit = init as { body?: BodyInit | null };
    const body = typeof requestInit.body === "string"
      ? JSON.parse(requestInit.body) as JsonObject
      : null;
    requests.push({ url, body });
    if (url.includes("/rest/v1/codex_sessions?select=metadata")) {
      return new Response(
        JSON.stringify([{ metadata: { codex_setup: existingSetup } }]),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }
    return new Response("", { status: 201 });
  };

  try {
    const response = await handleRequest(
      new Request("https://example.test/codex-session-ingest", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          version: 1,
          record: {
            id: "804fd832-7779-4665-9bec-2f10462c721b",
            type: "event",
            session_id: "session-setup",
            seq: 2,
            event_type: "tool_call_started",
            created_at: "2026-07-07T00:00:00.000Z",
            metadata: {
              cwd: "/repo",
              tool_name: "functions.exec_command",
              tool_phase: "started",
            },
          },
          event: {
            metadata: {
              tool_name: "functions.exec_command",
              tool_phase: "started",
            },
          },
          client: {
            repo_remote: "https://github.com/e3-solutions/codex-plugins.git",
            installation_id: "install-1",
          },
        }),
      }),
    );
    const sessionUpsert = requests.find((request) =>
      request.url.includes("/rest/v1/codex_sessions?on_conflict=")
    );
    const sessionMetadata = sessionUpsert?.body?.metadata as
      | JsonObject
      | undefined;

    assertEquals(response.status, 200);
    assertEquals(sessionMetadata?.codex_setup, existingSetup);
    assertEquals(sessionMetadata?.tool_name, "functions.exec_command");
  } finally {
    globalThis.fetch = originalFetch;
    restoreEnv("SUPABASE_URL", previousUrl);
    restoreEnv("SUPABASE_SERVICE_ROLE_KEY", previousServiceRole);
  }
});

Deno.test("handleRequest upserts session user identity rollup", async () => {
  const requests: Array<{ url: string; body: JsonObject | null }> = [];
  const originalFetch = globalThis.fetch;
  const previousUrl = Deno.env.get("SUPABASE_URL");
  const previousServiceRole = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");

  Deno.env.set("SUPABASE_URL", "https://project.supabase.co");
  Deno.env.set("SUPABASE_SERVICE_ROLE_KEY", "service-role-key");
  globalThis.fetch = async (input, init = {}) => {
    const url = input instanceof Request
      ? input.url
      : input instanceof URL
      ? input.toString()
      : input;
    const requestInit = init as { body?: BodyInit | null };
    const body = typeof requestInit.body === "string"
      ? JSON.parse(requestInit.body) as JsonObject
      : null;
    requests.push({ url, body });
    if (url.includes("/rest/v1/codex_sessions?select=metadata")) {
      return new Response(JSON.stringify([]), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    return new Response("", { status: 201 });
  };

  try {
    const response = await handleRequest(
      new Request("https://example.test/codex-session-ingest", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          version: 1,
          record: {
            id: "804fd832-7779-4665-9bec-2f10462c721b",
            session_id: "session-users",
            seq: 1,
            role: "user",
            content_sha256:
              "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
            content_byte_size: 5,
            content_excerpt: "hello",
            created_at: "2026-07-07T00:00:00.000Z",
            metadata: { cwd: "/repo" },
          },
          message: {
            content: "hello",
          },
          client: {
            repo_remote: "https://github.com/e3-solutions/codex-plugins.git",
            git_email: "priyal@example.test",
            git_user_name: "Priyal Taneja",
            linear_user_name: "Priyal",
            local_username: "priayltaneja",
            hostname: "e3s-MacBook-Air.local",
            installation_id: "2ae2052b-f419-47d5-b76a-fe5afdbe4394",
          },
        }),
      }),
    );
    const userUpsert = requests.find((request) =>
      request.url.includes("/rest/v1/codex_session_users?on_conflict=user_id")
    );
    const sessionUpsert = requests.find((request) =>
      request.url.includes("/rest/v1/codex_sessions?on_conflict=")
    );

    assertEquals(response.status, 200);
    assertEquals(userUpsert?.body, {
      user_id: sessionUpsert?.body?.user_id,
      first_seen_at: "2026-07-07T00:00:00.000Z",
      last_seen_at: "2026-07-07T00:00:00.000Z",
      git_email: "priyal@example.test",
      git_user_name: "Priyal Taneja",
      linear_user_name: "Priyal",
      local_username: "priayltaneja",
      hostname: "e3s-MacBook-Air.local",
      installation_id: "2ae2052b-f419-47d5-b76a-fe5afdbe4394",
    });
  } finally {
    globalThis.fetch = originalFetch;
    restoreEnv("SUPABASE_URL", previousUrl);
    restoreEnv("SUPABASE_SERVICE_ROLE_KEY", previousServiceRole);
  }
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

Deno.test("sanitizeEventPayload keeps safe Claude thread metadata only", () => {
  const sanitized = sanitizeEventPayload(
    {
      id: "a04fd832-7779-4665-9bec-2f10462c721b",
      session_id: "claude-session",
      seq: 2,
      event_type: "thread_prompt_submitted",
      hook_event_name: "UserPromptSubmit",
      created_at: "2026-07-06T00:00:00.000Z",
      metadata: {
        cwd: "/repo",
        platform: "claude-code",
        permission_mode: "acceptEdits",
        thread_event: "prompt_submitted",
        prompt_sha256: "9f86d081884c7d659a2feaa0c55ad015",
        prompt_byte_size: 44,
        prompt: "secret prompt should not store",
        tool_input: { command: "echo should-not-store" },
        arbitrary_secret: "sk-should-not-store",
      },
    },
    {
      metadata: {
        prompt: "event prompt should not store",
        arbitrary_secret: "sk-should-not-store",
      },
    },
  );
  const serialized = JSON.stringify(sanitized);

  assertEquals(sanitized.metadata, {
    cwd: "/repo",
    platform: "claude-code",
    permission_mode: "acceptEdits",
    prompt_byte_size: 44,
    thread_event: "prompt_submitted",
    prompt_sha256: "9f86d081884c7d659a2feaa0c55ad015",
  });
  assertNotIncludes(serialized, "secret prompt");
  assertNotIncludes(serialized, "tool_input");
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

function restoreEnv(name: string, previous: string | undefined): void {
  if (previous === undefined) {
    Deno.env.delete(name);
    return;
  }
  Deno.env.set(name, previous);
}
