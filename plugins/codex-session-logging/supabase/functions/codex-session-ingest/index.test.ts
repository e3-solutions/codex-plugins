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

Deno.test("handleRequest ignores historical backfill records without writes", async () => {
  const originalFetch = globalThis.fetch;
  let fetchCalls = 0;
  globalThis.fetch = () => {
    fetchCalls += 1;
    return Promise.resolve(new Response("", { status: 201 }));
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
            session_id: "historical-session",
            type: "message",
            hook_event_name: "HistoricalBackfill",
            metadata: { source: "historical_transcript" },
          },
          client: {
            repo_remote: "https://github.com/e3-solutions/codex-plugins.git",
            installation_id: "install-1",
          },
        }),
      }),
    );

    assertEquals(response.status, 200);
    assertEquals(await response.json(), {
      ok: true,
      ignored: true,
      reason: "historical_backfill_disabled",
    });
    assertEquals(fetchCalls, 0);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

Deno.test("handleRequest ignores historical backfill status without writes", async () => {
  const originalFetch = globalThis.fetch;
  let fetchCalls = 0;
  globalThis.fetch = () => {
    fetchCalls += 1;
    return Promise.resolve(new Response("", { status: 201 }));
  };

  try {
    const response = await handleRequest(
      new Request("https://example.test/codex-session-ingest", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify({
          version: 1,
          kind: "backfill_status",
          client: {
            repo_remote: "https://github.com/e3-solutions/codex-plugins.git",
            installation_id: "install-1",
          },
          backfill: {
            version: 1,
            status: "running",
            updated_at: "2026-07-13T16:30:00.000Z",
          },
        }),
      }),
    );

    assertEquals(response.status, 200);
    assertEquals(await response.json(), {
      ok: true,
      ignored: true,
      reason: "historical_backfill_disabled",
    });
    assertEquals(fetchCalls, 0);
  } finally {
    globalThis.fetch = originalFetch;
  }
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
  const existingTranscriptPath = "/sessions/thread.jsonl";

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
    if (url.includes("/rest/v1/codex_sessions?select=")) {
      return new Response(
        JSON.stringify([{
          metadata: {
            codex_setup: existingSetup,
            transcript_path: existingTranscriptPath,
          },
          thread_id: "existing-thread",
          started_at: "2026-07-01T00:00:00.000Z",
        }]),
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
    assertEquals(sessionUpsert?.body?.thread_id, "existing-thread");
    assertEquals(
      sessionUpsert?.body?.started_at,
      "2026-07-01T00:00:00.000Z",
    );
    assertEquals(sessionMetadata?.codex_setup, existingSetup);
    assertEquals(sessionMetadata?.transcript_path, existingTranscriptPath);
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
    if (url.includes("/rest/v1/codex_sessions?select=")) {
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
            thread_id: "thread-users",
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
    assertEquals(sessionUpsert?.body?.thread_id, "thread-users");
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

Deno.test("handleRequest still upserts non-backfill session token usage", async () => {
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
    if (url.includes("/rest/v1/codex_sessions?select=")) {
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
            type: "usage",
            session_id: "session-usage",
            thread_id: "thread-usage",
            created_at: "2026-07-07T00:00:00.000Z",
            metadata: { source: "live_session" },
          },
          usage: {
            input_tokens: 4090,
            cached_input_tokens: 1024,
            output_tokens: 52,
            reasoning_output_tokens: 8,
            total_tokens: 4142,
            model_context_window: 258400,
            created_at: "2026-07-07T00:00:00.000Z",
            metadata: { source: "live_session" },
          },
          client: {
            repo_remote: "https://github.com/e3-solutions/codex-plugins.git",
            installation_id: "install-1",
          },
        }),
      }),
    );
    const usageUpsert = requests.find((request) =>
      request.url.includes(
        "/rest/v1/codex_session_usage?on_conflict=session_id",
      )
    );

    assertEquals(response.status, 200);
    assertEquals(usageUpsert?.body?.session_id, "session-usage");
    assertEquals(usageUpsert?.body?.input_tokens, 4090);
    assertEquals(usageUpsert?.body?.cached_input_tokens, 1024);
    assertEquals(usageUpsert?.body?.output_tokens, 52);
    assertEquals(usageUpsert?.body?.reasoning_output_tokens, 8);
    assertEquals(usageUpsert?.body?.total_tokens, 4142);
    assertEquals(usageUpsert?.body?.model_context_window, 258400);
  } finally {
    globalThis.fetch = originalFetch;
    restoreEnv("SUPABASE_URL", previousUrl);
    restoreEnv("SUPABASE_SERVICE_ROLE_KEY", previousServiceRole);
  }
});

Deno.test("handleRequest stores rollout bytes and catalogs retries idempotently", async () => {
  const requests: Array<{
    url: string;
    body: JsonObject | null;
    rawBody: Uint8Array | null;
    contentType: string | null;
  }> = [];
  const originalFetch = globalThis.fetch;
  const previousUrl = Deno.env.get("SUPABASE_URL");
  const previousServiceRole = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  const rollout =
    '{"type":"event_msg","payload":{"message":"secret tool output"}}\n';
  const payload = await rolloutChunkPayload(rollout);

  Deno.env.set("SUPABASE_URL", "https://project.supabase.co");
  Deno.env.set("SUPABASE_SERVICE_ROLE_KEY", "service-role-key");
  globalThis.fetch = async (input, init = {}) => {
    const url = input instanceof Request
      ? input.url
      : input instanceof URL
      ? input.toString()
      : input;
    const requestInit = init as {
      headers?: HeadersInit;
      body?: BodyInit | null;
    };
    const headers = new Headers(requestInit.headers);
    const contentType = headers.get("content-type");
    const rawBody = requestInit.body === undefined || requestInit.body === null
      ? null
      : new Uint8Array(await new Response(requestInit.body).arrayBuffer());
    let body: JsonObject | null = null;
    if (rawBody && contentType === "application/json") {
      body = JSON.parse(new TextDecoder().decode(rawBody)) as JsonObject;
    }
    requests.push({ url, body, rawBody, contentType });
    if (url.includes("/rest/v1/codex_sessions?select=")) {
      return new Response(
        JSON.stringify([{
          metadata: {
            thread_source: "subagent",
            parent_thread_id: "22222222-2222-4222-8222-222222222222",
            durable_existing_field: "keep-me",
            client: { git_user_name: "Existing Name" },
          },
          thread_id: "existing-thread",
        }]),
        {
          status: 200,
          headers: { "content-type": "application/json" },
        },
      );
    }
    return new Response("", { status: 201 });
  };

  try {
    const request = () =>
      handleRequest(
        new Request("https://example.test/codex-session-ingest", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(payload),
        }),
      );
    const firstResponse = await request();
    const firstBody = await firstResponse.json();
    const secondResponse = await request();
    const secondBody = await secondResponse.json();
    const storageRequests = requests.filter((entry) =>
      entry.url.includes("/storage/v1/object/")
    );
    const eventUpserts = requests.filter((entry) =>
      entry.url.includes("/rest/v1/codex_session_events?on_conflict=id")
    );
    const sessionUpsert = requests.find((entry) =>
      entry.url.includes("/rest/v1/codex_sessions?on_conflict=id")
    );
    const eventMetadata = eventUpserts[0]?.body?.metadata as
      | JsonObject
      | undefined;
    const sessionMetadata = sessionUpsert?.body?.metadata as
      | JsonObject
      | undefined;
    const sessionClient = sessionMetadata?.client as JsonObject | undefined;

    assertEquals(firstResponse.status, 200);
    assertEquals(secondResponse.status, 200);
    assertEquals(firstBody.id, secondBody.id);
    assertEquals(firstBody.storage_path, secondBody.storage_path);
    assertIncludes(
      firstBody.storage_path,
      "/rollouts/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/0-",
    );
    assertEquals(storageRequests.length, 2);
    assertEquals(storageRequests[0]?.url, storageRequests[1]?.url);
    assertEquals(storageRequests[0]?.contentType, "application/x-ndjson");
    assertEquals(
      new TextDecoder().decode(storageRequests[0]?.rawBody ?? new Uint8Array()),
      rollout,
    );
    assertEquals(eventUpserts.length, 2);
    assertEquals(eventUpserts[0]?.body?.id, eventUpserts[1]?.body?.id);
    assertEquals(eventUpserts[0]?.body?.event_type, "rollout_chunk");
    assertEquals(eventMetadata?.file_generation, "a".repeat(32));
    assertEquals(eventMetadata?.start_offset, 0);
    assertEquals(
      eventMetadata?.end_offset,
      new TextEncoder().encode(rollout).byteLength,
    );
    assertEquals(eventMetadata?.thread_source, "subagent");
    assertEquals(eventMetadata?.rollout_source_category, "subagent.guardian");
    assertEquals(
      eventMetadata?.parent_thread_id,
      "22222222-2222-4222-8222-222222222222",
    );
    assertEquals(
      eventMetadata?.root_thread_id,
      "33333333-3333-4333-8333-333333333333",
    );
    assertNotIncludes(JSON.stringify(eventUpserts[0]?.body), "content_base64");
    assertNotIncludes(
      JSON.stringify(eventUpserts[0]?.body),
      "secret tool output",
    );
    assertEquals(sessionMetadata?.durable_existing_field, "keep-me");
    assertEquals(sessionUpsert?.body?.thread_id, "existing-thread");
    assertEquals(
      sessionUpsert?.body?.started_at,
      "2026-07-23T00:00:00.000Z",
    );
    assertEquals(sessionClient?.git_user_name, "Existing Name");
    assertEquals(sessionClient?.installation_id, "install-1");
  } finally {
    globalThis.fetch = originalFetch;
    restoreEnv("SUPABASE_URL", previousUrl);
    restoreEnv("SUPABASE_SERVICE_ROLE_KEY", previousServiceRole);
  }
});

Deno.test("handleRequest rejects malformed rollout chunks before writes", async () => {
  const originalFetch = globalThis.fetch;
  let fetchCalls = 0;
  globalThis.fetch = () => {
    fetchCalls += 1;
    return Promise.resolve(new Response("", { status: 201 }));
  };

  try {
    const valid = await rolloutChunkPayload("one complete line\n");
    const cases: Array<
      { name: string; mutate: (payload: JsonObject) => void }
    > = [
      {
        name: "non-canonical session UUID",
        mutate: (payload) => {
          optionalTestObject(payload.record).session_id = "not-a-uuid";
        },
      },
      {
        name: "non-hex generation",
        mutate: (payload) => {
          optionalTestObject(payload.rollout_chunk).file_generation =
            "generation";
        },
      },
      {
        name: "invalid offset range",
        mutate: (payload) => {
          optionalTestObject(payload.rollout_chunk).end_offset = 0;
        },
      },
      {
        name: "oversized content",
        mutate: (payload) => {
          const chunk = optionalTestObject(payload.rollout_chunk);
          chunk.end_offset = 1024 * 1024 + 1;
          chunk.content_byte_size = 1024 * 1024 + 1;
        },
      },
      {
        name: "invalid SHA-256",
        mutate: (payload) => {
          optionalTestObject(payload.rollout_chunk).content_sha256 = "0".repeat(
            64,
          );
        },
      },
      {
        name: "invalid base64",
        mutate: (payload) => {
          optionalTestObject(payload.rollout_chunk).content_base64 =
            "not base64";
        },
      },
    ];

    for (const testCase of cases) {
      const payload = structuredClone(valid);
      testCase.mutate(payload);
      const response = await handleRequest(
        new Request("https://example.test/codex-session-ingest", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(payload),
        }),
      );
      const body = await response.json();
      assertEquals(response.status, 400);
      assertEquals(body.error, "invalid_payload");
      assertEquals(typeof body.message, "string");
    }
    assertEquals(fetchCalls, 0);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

Deno.test("sanitizeEventPayload keeps only allowlisted tool event fields", () => {
  const sanitized = sanitizeEventPayload(
    {
      id: "804fd832-7779-4665-9bec-2f10462c721b",
      session_id: "session-tools",
      thread_id: "thread-tools",
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
    thread_id: "thread-tools",
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

Deno.test("sanitizeEventPayload keeps resident presence metadata content-free", () => {
  const sanitized = sanitizeEventPayload(
    {
      id: "resident-presence-id",
      session_id: "codex-thread-id",
      thread_id: "stable-thread-id",
      seq: 0,
      event_type: "resident_presence",
      hook_event_name: "ResidentPresence",
      created_at: "2026-07-14T18:20:00.000Z",
      metadata: {
        cwd: "/repo",
        transcript_path: "/codex/rollout.jsonl",
        source: "resident_presence",
        repo_remote: "https://github.com/e3-solutions/example.git",
        git_branch: "arya/example",
        native_created_at: "2026-07-14T18:00:00.000Z",
        native_updated_at: "2026-07-14T18:20:00.000Z",
        thread_source: "subagent",
        parent_thread_id: "parent-codex-thread-id",
        agent_nickname: "sensitive nickname",
        title: "sensitive title",
        preview: "sensitive preview",
        prompt: "sensitive prompt",
        content: "sensitive response",
      },
    },
    { metadata: {} },
  );
  const serialized = JSON.stringify(sanitized);

  assertEquals(sanitized, {
    id: "resident-presence-id",
    session_id: "codex-thread-id",
    thread_id: "stable-thread-id",
    seq: 0,
    event_type: "resident_presence",
    hook_event_name: "ResidentPresence",
    created_at: "2026-07-14T18:20:00.000Z",
    metadata: {
      cwd: "/repo",
      transcript_path: "/codex/rollout.jsonl",
      source: "resident_presence",
      thread_source: "subagent",
      parent_thread_id: "parent-codex-thread-id",
    },
  });
  for (
    const forbidden of [
      "repo_remote",
      "git_branch",
      "native_created_at",
      "native_updated_at",
      "sensitive nickname",
      "sensitive title",
      "sensitive preview",
      "sensitive prompt",
      "sensitive response",
    ]
  ) {
    assertNotIncludes(serialized, forbidden);
  }
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

Deno.test("handleRequest stamps the claude agent and end time onto the session", async () => {
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
    if (url.includes("/rest/v1/codex_sessions?select=")) {
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
            id: "b04fd832-7779-4665-9bec-2f10462c721b",
            type: "event",
            session_id: "claude-session-end",
            seq: 9,
            event_type: "thread_stopped",
            hook_event_name: "Stop",
            created_at: "2026-07-16T00:00:00.000Z",
            ended_at: "2026-07-16T00:00:00.000Z",
            metadata: {
              cwd: "/repo",
              platform: "claude-code",
              agent: "claude",
              thread_event: "stopped",
            },
          },
          event: {
            metadata: {
              cwd: "/repo",
              platform: "claude-code",
              agent: "claude",
              thread_event: "stopped",
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
    assertEquals(sessionMetadata?.agent, "claude");
    assertEquals(sessionUpsert?.body?.ended_at, "2026-07-16T00:00:00.000Z");
  } finally {
    globalThis.fetch = originalFetch;
    restoreEnv("SUPABASE_URL", previousUrl);
    restoreEnv("SUPABASE_SERVICE_ROLE_KEY", previousServiceRole);
  }
});

Deno.test("handleRequest defaults agent to codex and clears ended_at on live events", async () => {
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
    if (url.includes("/rest/v1/codex_sessions?select=")) {
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
            id: "c04fd832-7779-4665-9bec-2f10462c721b",
            type: "event",
            session_id: "codex-session-live",
            seq: 3,
            event_type: "tool_call_started",
            created_at: "2026-07-16T00:00:00.000Z",
            metadata: {
              cwd: "/repo",
              tool_name: "shell",
              tool_phase: "started",
            },
          },
          event: {
            metadata: { tool_name: "shell", tool_phase: "started" },
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
    assertEquals(sessionMetadata?.agent, "codex");
    assertEquals(sessionUpsert?.body?.ended_at, null);
  } finally {
    globalThis.fetch = originalFetch;
    restoreEnv("SUPABASE_URL", previousUrl);
    restoreEnv("SUPABASE_SERVICE_ROLE_KEY", previousServiceRole);
  }
});

async function rolloutChunkPayload(content: string): Promise<JsonObject> {
  const bytes = new TextEncoder().encode(content);
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  const contentSha256 = Array.from(new Uint8Array(digest)).map((byte) =>
    byte.toString(16).padStart(2, "0")
  ).join("");
  const binary = Array.from(bytes, (byte) => String.fromCharCode(byte)).join(
    "",
  );
  return {
    version: 1,
    kind: "rollout_chunk",
    record: {
      type: "event",
      event_type: "rollout_chunk",
      session_id: "11111111-1111-4111-8111-111111111111",
      thread_id: "stable-thread-id",
      created_at: "2026-07-23T00:00:00.000Z",
      metadata: {
        cwd: "/repo",
        transcript_path: "/codex/rollout.jsonl",
        thread_source: "subagent",
        rollout_source_category: "subagent.guardian",
        parent_thread_id: "22222222-2222-4222-8222-222222222222",
        root_thread_id: "33333333-3333-4333-8333-333333333333",
        arbitrary_secret: "must-not-catalog",
      },
    },
    rollout_chunk: {
      file_generation: "a".repeat(32),
      start_offset: 0,
      end_offset: bytes.byteLength,
      content_byte_size: bytes.byteLength,
      content_sha256: contentSha256,
      content_base64: btoa(binary),
    },
    client: {
      repo_remote: "https://github.com/e3-solutions/codex-plugins.git",
      installation_id: "install-1",
    },
  };
}

function optionalTestObject(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as JsonObject
    : {};
}

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
