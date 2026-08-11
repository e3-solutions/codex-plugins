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

Deno.test("handleRequest discards a fenced rollout and removes its object", async () => {
  const originalFetch = globalThis.fetch;
  const previousUrl = Deno.env.get("SUPABASE_URL");
  const previousServiceRole = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  const requests: Array<{ url: string; method: string; body: string | null }> =
    [];
  Deno.env.set("SUPABASE_URL", "https://project.supabase.co");
  Deno.env.set("SUPABASE_SERVICE_ROLE_KEY", "service-role-key");
  globalThis.fetch = (input, init = {}) => {
    const url = input instanceof Request
      ? input.url
      : input instanceof URL
      ? input.toString()
      : input;
    const requestInit = init as { method?: string; body?: BodyInit | null };
    requests.push({
      url,
      method: requestInit.method ?? "GET",
      body: typeof requestInit.body === "string" ? requestInit.body : null,
    });
    return Promise.resolve(
      new Response(
        JSON.stringify([{
          session_id_hash: "a".repeat(64),
        }]),
        {
          status: 200,
          headers: { "content-type": "application/json" },
        },
      ),
    );
  };

  try {
    const payload = await rolloutChunkPayload("sensitive rollout\n");
    const response = await handleRequest(
      new Request("https://example.test/codex-session-ingest", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
      }),
    );

    assertEquals(response.status, 200);
    assertEquals(await response.json(), {
      ok: true,
      id: optionalTestObject(payload.record).id,
      ignored: true,
      reason: "session_opted_out",
    });
    assertEquals(requests.length, 3);
    assertIncludes(requests[0].url, "/rest/v1/codex_session_users?");
    assertEquals(requests[0].method, "GET");
    assertIncludes(requests[1].url, "/rest/v1/codex_ignored_sessions?");
    const expectedSessionHash =
      "e964072a95ff700a5dfb7ea1d1632a5fa672eee28b2a5ea2680eb72adb927928";
    assertEquals(
      await testSha256Hex(
        "codex_rollout_session:11111111-1111-4111-8111-111111111111",
      ),
      expectedSessionHash,
    );
    assertIncludes(requests[1].url, expectedSessionHash);
    assertEquals(requests[1].method, "GET");
    assertEquals(
      requests[2].url,
      "https://project.supabase.co/storage/v1/object/codex-sessions",
    );
    assertEquals(requests[2].method, "DELETE");
    const deleteBody = JSON.parse(requests[2].body ?? "{}") as JsonObject;
    const prefixes = deleteBody.prefixes as unknown[];
    assertEquals(prefixes.length, 1);
    assertIncludes(
      String(prefixes[0]),
      "/sessions/11111111-1111-4111-8111-111111111111/",
    );
  } finally {
    globalThis.fetch = originalFetch;
    restoreEnv("SUPABASE_URL", previousUrl);
    restoreEnv("SUPABASE_SERVICE_ROLE_KEY", previousServiceRole);
  }
});

Deno.test("handleRequest removes a rollout fenced during its catalog writes", async () => {
  const originalFetch = globalThis.fetch;
  const previousUrl = Deno.env.get("SUPABASE_URL");
  const previousServiceRole = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  const requests: Array<{ url: string; method: string }> = [];
  let fenceChecks = 0;
  Deno.env.set("SUPABASE_URL", "https://project.supabase.co");
  Deno.env.set("SUPABASE_SERVICE_ROLE_KEY", "service-role-key");
  globalThis.fetch = (input, init = {}) => {
    const url = input instanceof Request
      ? input.url
      : input instanceof URL
      ? input.toString()
      : input;
    const requestInit = init as { method?: string };
    const method = requestInit.method ?? "GET";
    requests.push({ url, method });
    if (url.includes("/rest/v1/rpc/reserve_codex_session_storage")) {
      return Promise.resolve(reservedStorageResponse());
    }
    if (url.includes("/rest/v1/codex_ignored_sessions?")) {
      fenceChecks += 1;
      return Promise.resolve(
        new Response(
          JSON.stringify(
            fenceChecks === 1 ? [] : [{ session_id_hash: "a".repeat(64) }],
          ),
          {
            status: 200,
            headers: { "content-type": "application/json" },
          },
        ),
      );
    }
    if (url.includes("/rest/v1/codex_session_users?")) {
      return Promise.resolve(
        new Response(
          JSON.stringify([{
            user_id: "99999999-9999-4999-8999-999999999999",
            git_email: "owner@example.com",
            first_seen_at: "2026-07-01T00:00:00.000Z",
          }]),
          {
            status: 200,
            headers: { "content-type": "application/json" },
          },
        ),
      );
    }
    if (url.includes("/rest/v1/codex_sessions?select=")) {
      return Promise.resolve(
        new Response(JSON.stringify([]), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
    }
    return Promise.resolve(new Response("", { status: 201 }));
  };

  try {
    const payload = await rolloutChunkPayload("sensitive rollout\n");
    const response = await handleRequest(
      new Request("https://example.test/codex-session-ingest", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
      }),
    );
    const body = await response.json();

    assertEquals(response.status, 200);
    assertEquals(body.ignored, true);
    assertEquals(fenceChecks, 2);
    assertEquals(
      requests.filter((request) =>
        request.method === "POST" &&
        request.url.includes("/storage/v1/object/")
      ).length,
      1,
    );
    assertEquals(
      requests.filter((request) =>
        request.method === "DELETE" &&
        request.url.includes("/storage/v1/object/")
      ).length,
      1,
    );
  } finally {
    globalThis.fetch = originalFetch;
    restoreEnv("SUPABASE_URL", previousUrl);
    restoreEnv("SUPABASE_SERVICE_ROLE_KEY", previousServiceRole);
  }
});

Deno.test("handleRequest preserves a reserved object when cataloging fails", async () => {
  const originalFetch = globalThis.fetch;
  const previousUrl = Deno.env.get("SUPABASE_URL");
  const previousServiceRole = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  const requests: Array<{ url: string; method: string }> = [];
  Deno.env.set("SUPABASE_URL", "https://project.supabase.co");
  Deno.env.set("SUPABASE_SERVICE_ROLE_KEY", "service-role-key");
  globalThis.fetch = (input, init = {}) => {
    const url = input instanceof Request
      ? input.url
      : input instanceof URL
      ? input.toString()
      : input;
    const method = (init as { method?: string }).method ?? "GET";
    requests.push({ url, method });
    if (url.includes("/rest/v1/rpc/reserve_codex_session_storage")) {
      return Promise.resolve(reservedStorageResponse());
    }
    if (url.includes("/rest/v1/codex_ignored_sessions?")) {
      return Promise.resolve(
        new Response(JSON.stringify([]), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
    }
    if (url.includes("/rest/v1/codex_session_users?")) {
      return Promise.resolve(
        new Response(
          JSON.stringify([{
            user_id: "99999999-9999-4999-8999-999999999999",
          }]),
          {
            status: 200,
            headers: { "content-type": "application/json" },
          },
        ),
      );
    }
    if (url.includes("/rest/v1/codex_sessions?select=")) {
      return Promise.resolve(
        new Response(JSON.stringify([]), {
          status: 200,
          headers: { "content-type": "application/json" },
        }),
      );
    }
    if (
      method === "POST" &&
      url.includes("/rest/v1/codex_sessions?on_conflict=")
    ) {
      return Promise.resolve(new Response("catalog conflict", { status: 409 }));
    }
    return Promise.resolve(new Response("", { status: 201 }));
  };

  try {
    const payload = await rolloutChunkPayload("catalog race\n");
    const response = await handleRequest(
      new Request("https://example.test/codex-session-ingest", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(payload),
      }),
    );

    assertEquals(response.status, 500);
    assertEquals(
      requests.filter((request) =>
        request.method === "DELETE" &&
        request.url.endsWith("/storage/v1/object/codex-sessions")
      ).length,
      0,
    );
  } finally {
    globalThis.fetch = originalFetch;
    restoreEnv("SUPABASE_URL", previousUrl);
    restoreEnv("SUPABASE_SERVICE_ROLE_KEY", previousServiceRole);
  }
});

Deno.test("handleRequest reserves storage before the first upload", async () => {
  const originalFetch = globalThis.fetch;
  const previousUrl = Deno.env.get("SUPABASE_URL");
  const previousServiceRole = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  let storageUploads = 0;
  Deno.env.set("SUPABASE_URL", "https://project.supabase.co");
  Deno.env.set("SUPABASE_SERVICE_ROLE_KEY", "service-role-key");
  globalThis.fetch = (input, init = {}) => {
    const url = input instanceof Request
      ? input.url
      : input instanceof URL
      ? input.toString()
      : input;
    if (url.includes("/rest/v1/codex_ignored_sessions?")) {
      return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }));
    }
    if (url.includes("/rest/v1/codex_session_users?")) {
      return Promise.resolve(
        new Response(
          JSON.stringify([{
            user_id: "99999999-9999-4999-8999-999999999999",
          }]),
          { status: 200 },
        ),
      );
    }
    if (url.includes("/rest/v1/codex_sessions?select=")) {
      return Promise.resolve(new Response(JSON.stringify([]), { status: 200 }));
    }
    if (url.includes("/rest/v1/rpc/reserve_codex_session_storage")) {
      return Promise.resolve(
        new Response("storage identity conflict", {
          status: 409,
        }),
      );
    }
    if (
      (init as { method?: string }).method === "POST" &&
      url.includes("/storage/v1/object/")
    ) {
      storageUploads += 1;
    }
    return Promise.resolve(new Response("", { status: 201 }));
  };

  try {
    const response = await handleRequest(
      new Request("https://example.test/codex-session-ingest", {
        method: "POST",
        headers: { "content-type": "application/json" },
        body: JSON.stringify(await rolloutChunkPayload("reservation race\n")),
      }),
    );
    assertEquals(response.status, 500);
    assertEquals(storageUploads, 0);
  } finally {
    globalThis.fetch = originalFetch;
    restoreEnv("SUPABASE_URL", previousUrl);
    restoreEnv("SUPABASE_SERVICE_ROLE_KEY", previousServiceRole);
  }
});

Deno.test("handleRequest preserves existing session codex setup on later event upserts", async () => {
  const requests: Array<{ url: string; body: JsonObject | null }> = [];
  const originalFetch = globalThis.fetch;
  const previousUrl = Deno.env.get("SUPABASE_URL");
  const previousServiceRole = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  const existingUserId = "11111111-1111-4111-8111-111111111111";
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
    if (url.includes("/rest/v1/rpc/reserve_codex_session_storage")) {
      return reservedStorageResponse();
    }
    if (url.includes("/rest/v1/codex_ignored_sessions?")) {
      return new Response(JSON.stringify([]), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    if (url.includes("/rest/v1/codex_session_users?select=")) {
      return new Response(JSON.stringify([{ user_id: existingUserId }]), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    if (url.includes("/rest/v1/codex_sessions?select=")) {
      return new Response(
        JSON.stringify([{
          metadata: {
            codex_setup: existingSetup,
            transcript_path: existingTranscriptPath,
          },
          thread_id: "existing-thread",
          started_at: "2026-07-01T00:00:00.000Z",
          user_id: existingUserId,
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
    if (url.includes("/rest/v1/rpc/reserve_codex_session_storage")) {
      return reservedStorageResponse();
    }
    if (
      url.includes("/rest/v1/codex_ignored_sessions?") ||
      url.includes("/rest/v1/codex_sessions?select=") ||
      url.includes("/rest/v1/codex_session_users?select=")
    ) {
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
    assertEquals(
      sessionUpsert?.body?.installation_capability_sha256,
      await testSha256Hex("2ae2052b-f419-47d5-b76a-fe5afdbe4394"),
    );
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

Deno.test("handleRequest reuses the email-backed user for an existing installation", async () => {
  const requests: Array<{ url: string; body: JsonObject | null }> = [];
  const originalFetch = globalThis.fetch;
  const previousUrl = Deno.env.get("SUPABASE_URL");
  const previousServiceRole = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  const canonicalUserId = "11111111-1111-4111-8111-111111111111";

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
    if (url.includes("/rest/v1/rpc/reserve_codex_session_storage")) {
      return reservedStorageResponse();
    }
    if (url.includes("/rest/v1/codex_ignored_sessions?")) {
      return new Response(JSON.stringify([]), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    if (url.includes("/rest/v1/codex_session_users?select=")) {
      return new Response(
        JSON.stringify([
          {
            user_id: "22222222-2222-4222-8222-222222222222",
            git_email: null,
            first_seen_at: "2026-07-01T00:00:00.000Z",
          },
          {
            user_id: canonicalUserId,
            git_email: "developer@example.test",
            first_seen_at: "2026-06-01T00:00:00.000Z",
          },
        ]),
        { status: 200, headers: { "content-type": "application/json" } },
      );
    }
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
            id: "904fd832-7779-4665-9bec-2f10462c721b",
            type: "event",
            session_id: "missing-email-session",
            seq: 1,
            event_type: "resident_presence",
            created_at: "2026-07-22T00:00:00.000Z",
            metadata: { cwd: "/repo" },
          },
          event: { metadata: { cwd: "/repo" } },
          client: {
            repo_remote: "https://github.com/e3-solutions/codex-plugins.git",
            installation_id: "shared-installation-id",
            local_username: "developer",
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
    const eventUpsert = requests.find((request) =>
      request.url.includes("/rest/v1/codex_session_events?on_conflict=")
    );

    assertEquals(response.status, 200);
    assertEquals(userUpsert?.body?.user_id, canonicalUserId);
    assertEquals(sessionUpsert?.body?.user_id, canonicalUserId);
    assertEquals(eventUpsert?.body?.user_id, canonicalUserId);
  } finally {
    globalThis.fetch = originalFetch;
    restoreEnv("SUPABASE_URL", previousUrl);
    restoreEnv("SUPABASE_SERVICE_ROLE_KEY", previousServiceRole);
  }
});

Deno.test("handleRequest accepts usage with matching capability and configured token", async () => {
  const requests: Array<{ url: string; body: JsonObject | null }> = [];
  const originalFetch = globalThis.fetch;
  const previousUrl = Deno.env.get("SUPABASE_URL");
  const previousServiceRole = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  const previousIngestToken = Deno.env.get("CODEX_SESSION_LOG_INGEST_TOKEN");
  const installationId = "2ae2052b-f419-47d5-b76a-fe5afdbe4394";
  const capability = await testSha256Hex(installationId);

  Deno.env.set("SUPABASE_URL", "https://project.supabase.co");
  Deno.env.set("SUPABASE_SERVICE_ROLE_KEY", "service-role-key");
  Deno.env.set("CODEX_SESSION_LOG_INGEST_TOKEN", "global-secret");
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
    if (url.includes("/rest/v1/codex_ignored_sessions?")) {
      return new Response(JSON.stringify([]), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    if (url.includes("/rest/v1/codex_sessions?select=")) {
      return new Response(
        JSON.stringify([{
          installation_capability_sha256: capability,
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
    const response = await handleRequest(
      new Request("https://example.test/codex-session-ingest", {
        method: "POST",
        headers: {
          "content-type": "application/json",
          "x-codex-session-log-token": "global-secret",
        },
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
            input_tokens: 3066,
            cached_input_tokens: 1024,
            output_tokens: 44,
            reasoning_output_tokens: 8,
            total_tokens: 4142,
            model_context_window: 258400,
            created_at: "2026-07-07T00:00:00.000Z",
            metadata: { source: "live_session" },
          },
          client: {
            repo_remote: "https://github.com/e3-solutions/codex-plugins.git",
            installation_id: installationId,
          },
        }),
      }),
    );
    const usageUpsert = requests.find((request) =>
      request.url.includes(
        "/rest/v1/rpc/upsert_codex_session_usage_latest",
      )
    );

    assertEquals(response.status, 200);
    assertEquals(usageUpsert?.body?.p_session_id, "session-usage");
    assertEquals(usageUpsert?.body?.p_input_tokens, 3066);
    assertEquals(usageUpsert?.body?.p_cached_input_tokens, 1024);
    assertEquals(usageUpsert?.body?.p_output_tokens, 44);
    assertEquals(usageUpsert?.body?.p_reasoning_output_tokens, 8);
    assertEquals(usageUpsert?.body?.p_total_tokens, 4142);
    assertEquals(usageUpsert?.body?.p_model_context_window, 258400);
    assertEquals(usageUpsert?.body?.p_user_id, undefined);
    assertEquals(
      usageUpsert?.body?.p_observed_at,
      "2026-07-07T00:00:00.000Z",
    );
  } finally {
    globalThis.fetch = originalFetch;
    restoreEnv("SUPABASE_URL", previousUrl);
    restoreEnv("SUPABASE_SERVICE_ROLE_KEY", previousServiceRole);
    restoreEnv("CODEX_SESSION_LOG_INGEST_TOKEN", previousIngestToken);
  }
});

Deno.test("handleRequest rejects inconsistent usage before any write", async () => {
  const requests: Array<{ url: string; method: string }> = [];
  const originalFetch = globalThis.fetch;
  const previousUrl = Deno.env.get("SUPABASE_URL");
  const previousServiceRole = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  const previousIngestToken = Deno.env.get("CODEX_SESSION_LOG_INGEST_TOKEN");

  Deno.env.set("SUPABASE_URL", "https://project.supabase.co");
  Deno.env.set("SUPABASE_SERVICE_ROLE_KEY", "service-role-key");
  Deno.env.delete("CODEX_SESSION_LOG_INGEST_TOKEN");
  globalThis.fetch = async (input, init = {}) => {
    const url = input instanceof Request
      ? input.url
      : input instanceof URL
      ? input.toString()
      : input;
    const requestInit = init as { method?: string };
    requests.push({ url, method: String(requestInit.method ?? "GET") });
    return new Response(JSON.stringify([]), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
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
            created_at: "2026-07-07T00:00:00.000Z",
            metadata: { source: "live_session" },
          },
          usage: {
            input_tokens: 10,
            cached_input_tokens: 5,
            output_tokens: 2,
            reasoning_output_tokens: 1,
            total_tokens: 17,
            created_at: "2026-07-07T00:00:00.000Z",
          },
          client: {
            repo_remote: "https://github.com/e3-solutions/codex-plugins.git",
            installation_id: "2ae2052b-f419-47d5-b76a-fe5afdbe4394",
          },
        }),
      }),
    );

    assertEquals(response.status, 400);
    assertEquals(
      requests.filter((request) => request.method !== "GET"),
      [],
    );
  } finally {
    globalThis.fetch = originalFetch;
    restoreEnv("SUPABASE_URL", previousUrl);
    restoreEnv("SUPABASE_SERVICE_ROLE_KEY", previousServiceRole);
    restoreEnv("CODEX_SESSION_LOG_INGEST_TOKEN", previousIngestToken);
  }
});

Deno.test("handleRequest returns one retryable failure for usage capability errors", async () => {
  const originalFetch = globalThis.fetch;
  const previousUrl = Deno.env.get("SUPABASE_URL");
  const previousServiceRole = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  const previousIngestToken = Deno.env.get("CODEX_SESSION_LOG_INGEST_TOKEN");
  const installationId = "2ae2052b-f419-47d5-b76a-fe5afdbe4394";
  const otherCapability = await testSha256Hex(
    "11111111-1111-4111-8111-111111111111",
  );
  const scenarios: Array<{
    installationId?: string;
    rows: JsonObject[];
  }> = [
    { rows: [] },
    { installationId: "not-a-uuid", rows: [] },
    { installationId, rows: [] },
    { installationId, rows: [{}] },
    {
      installationId,
      rows: [{ installation_capability_sha256: otherCapability }],
    },
  ];

  Deno.env.set("SUPABASE_URL", "https://project.supabase.co");
  Deno.env.set("SUPABASE_SERVICE_ROLE_KEY", "service-role-key");
  Deno.env.delete("CODEX_SESSION_LOG_INGEST_TOKEN");
  try {
    for (const scenario of scenarios) {
      let writes = 0;
      globalThis.fetch = async (input, init = {}) => {
        const url = input instanceof Request
          ? input.url
          : input instanceof URL
          ? input.toString()
          : input;
        const requestInit = init as { method?: string };
        if (requestInit.method !== "GET") {
          writes += 1;
        }
        if (url.includes("/rest/v1/codex_ignored_sessions?")) {
          return new Response(JSON.stringify([]), {
            status: 200,
            headers: { "content-type": "application/json" },
          });
        }
        return new Response(JSON.stringify(scenario.rows), {
          status: 200,
          headers: { "content-type": "application/json" },
        });
      };
      const client: JsonObject = {
        repo_remote: "https://github.com/e3-solutions/codex-plugins.git",
      };
      if (scenario.installationId) {
        client.installation_id = scenario.installationId;
      }
      const response = await handleRequest(
        new Request("https://example.test/codex-session-ingest", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify({
            record: {
              type: "usage",
              session_id: "session-usage",
            },
            usage: {
              input_tokens: 1,
              cached_input_tokens: 0,
              output_tokens: 1,
              reasoning_output_tokens: 0,
              total_tokens: 2,
              created_at: "2026-07-07T00:00:00.000Z",
            },
            client,
          }),
        }),
      );

      assertEquals(response.status, 503);
      assertEquals(await response.json(), { error: "usage_ingest_retryable" });
      assertEquals(writes, 0);
    }
  } finally {
    globalThis.fetch = originalFetch;
    restoreEnv("SUPABASE_URL", previousUrl);
    restoreEnv("SUPABASE_SERVICE_ROLE_KEY", previousServiceRole);
    restoreEnv("CODEX_SESSION_LOG_INGEST_TOKEN", previousIngestToken);
  }
});

Deno.test("handleRequest retains the configured token check for all requests", async () => {
  const originalFetch = globalThis.fetch;
  const previousIngestToken = Deno.env.get("CODEX_SESSION_LOG_INGEST_TOKEN");
  let fetchCalls = 0;
  Deno.env.set("CODEX_SESSION_LOG_INGEST_TOKEN", "global-secret");
  globalThis.fetch = () => {
    fetchCalls += 1;
    return Promise.resolve(new Response("", { status: 201 }));
  };
  try {
    for (
      const payload of [
        { client: {} },
        { client: {}, record: { type: "usage" } },
      ]
    ) {
      const response = await handleRequest(
        new Request("https://example.test/codex-session-ingest", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(payload),
        }),
      );
      assertEquals(response.status, 401);
      assertEquals(await response.json(), { error: "invalid_ingest_token" });
    }
    assertEquals(fetchCalls, 0);
  } finally {
    globalThis.fetch = originalFetch;
    restoreEnv("CODEX_SESSION_LOG_INGEST_TOKEN", previousIngestToken);
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
    if (url.includes("/rest/v1/rpc/reserve_codex_session_storage")) {
      return reservedStorageResponse();
    }
    if (url.includes("/rest/v1/codex_ignored_sessions?")) {
      return new Response(JSON.stringify([]), {
        status: 200,
        headers: { "content-type": "application/json" },
      });
    }
    if (url.includes("/rest/v1/codex_session_users?select=user_id")) {
      return new Response(
        JSON.stringify([{
          user_id: "99999999-9999-4999-8999-999999999999",
          git_email: "owner@example.com",
          first_seen_at: "2026-07-01T00:00:00.000Z",
        }]),
        {
          status: 200,
          headers: { "content-type": "application/json" },
        },
      );
    }
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
          user_id: "99999999-9999-4999-8999-999999999999",
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
      "users/99999999-9999-4999-8999-999999999999/sessions/11111111-1111-4111-8111-111111111111/rollouts/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/0-",
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

Deno.test("handleRequest rejects existing event and rollout owner mismatches before writes", async () => {
  const originalFetch = globalThis.fetch;
  const previousUrl = Deno.env.get("SUPABASE_URL");
  const previousServiceRole = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  const previousIngestToken = Deno.env.get("CODEX_SESSION_LOG_INGEST_TOKEN");
  const eventPayload = {
    record: {
      id: "804fd832-7779-4665-9bec-2f10462c721b",
      type: "event",
      session_id: "existing-event-session",
      seq: 1,
      event_type: "tool_call_started",
      created_at: "2026-07-27T00:00:00.000Z",
      metadata: { tool_name: "shell", tool_phase: "started" },
    },
    event: { metadata: { tool_name: "shell", tool_phase: "started" } },
    client: {
      repo_remote: "https://github.com/e3-solutions/codex-plugins.git",
      installation_id: "install-1",
    },
  };
  const rolloutPayload = await rolloutChunkPayload("complete rollout line\n");
  let writes = 0;
  const existingUserId = "22222222-2222-4222-8222-222222222222";

  Deno.env.set("SUPABASE_URL", "https://project.supabase.co");
  Deno.env.set("SUPABASE_SERVICE_ROLE_KEY", "service-role-key");
  Deno.env.delete("CODEX_SESSION_LOG_INGEST_TOKEN");
  globalThis.fetch = async (input, init = {}) => {
    const url = input instanceof Request
      ? input.url
      : input instanceof URL
      ? input.toString()
      : input;
    const requestInit = init as { method?: string };
    if (url.includes("/rest/v1/rpc/reserve_codex_session_storage")) {
      return reservedStorageResponse();
    }
    if (requestInit.method !== "GET") {
      writes += 1;
    }
    const rows = url.includes("/rest/v1/codex_ignored_sessions?")
      ? []
      : url.includes("/rest/v1/codex_session_users?select=")
      ? [{
        user_id: "11111111-1111-4111-8111-111111111111",
        first_seen_at: "2026-07-01T00:00:00.000Z",
      }]
      : [{ user_id: existingUserId, storage_bucket: "codex-sessions" }];
    return new Response(JSON.stringify(rows), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  };

  try {
    for (const payload of [eventPayload, rolloutPayload]) {
      writes = 0;
      const response = await handleRequest(
        new Request("https://example.test/codex-session-ingest", {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: JSON.stringify(payload),
        }),
      );

      assertEquals(response.status, 422);
      assertEquals(await response.json(), { error: "session_rejected" });
      assertEquals(writes, 0);
    }
  } finally {
    globalThis.fetch = originalFetch;
    restoreEnv("SUPABASE_URL", previousUrl);
    restoreEnv("SUPABASE_SERVICE_ROLE_KEY", previousServiceRole);
    restoreEnv("CODEX_SESSION_LOG_INGEST_TOKEN", previousIngestToken);
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
    if (url.includes("/rest/v1/rpc/reserve_codex_session_storage")) {
      return reservedStorageResponse();
    }
    if (
      url.includes("/rest/v1/codex_ignored_sessions?") ||
      url.includes("/rest/v1/codex_sessions?select=") ||
      url.includes("/rest/v1/codex_session_users?select=")
    ) {
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
    if (url.includes("/rest/v1/rpc/reserve_codex_session_storage")) {
      return reservedStorageResponse();
    }
    if (
      url.includes("/rest/v1/codex_ignored_sessions?") ||
      url.includes("/rest/v1/codex_sessions?select=") ||
      url.includes("/rest/v1/codex_session_users?select=")
    ) {
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

async function testSha256Hex(value: string): Promise<string> {
  const digest = await crypto.subtle.digest(
    "SHA-256",
    new TextEncoder().encode(value),
  );
  return Array.from(new Uint8Array(digest)).map((byte) =>
    byte.toString(16).padStart(2, "0")
  ).join("");
}

function reservedStorageResponse(): Response {
  return new Response(JSON.stringify({ status: "reserved" }), {
    status: 200,
    headers: { "content-type": "application/json" },
  });
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
