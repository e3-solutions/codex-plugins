import {
  clientIdentityKey,
  userIdForClientIdentity,
} from "./client_identity.ts";

Deno.test("userIdForClientIdentity maps git email when configured", async () => {
  const previous = Deno.env.get("CODEX_SESSION_LOG_USER_EMAIL_MAP");
  Deno.env.set(
    "CODEX_SESSION_LOG_USER_EMAIL_MAP",
    '{"arya@e3.solutions":"11111111-1111-4111-8111-111111111111"}',
  );
  try {
    const userId = await userIdForClientIdentity({
      git_email: "Arya@E3.Solutions",
      installation_id: "install-should-not-win",
    });

    assertEquals(userId, "11111111-1111-4111-8111-111111111111");
  } finally {
    if (previous === undefined) {
      Deno.env.delete("CODEX_SESSION_LOG_USER_EMAIL_MAP");
    } else {
      Deno.env.set("CODEX_SESSION_LOG_USER_EMAIL_MAP", previous);
    }
  }
});

Deno.test("userIdForClientIdentity falls back to installation id without git email", async () => {
  const previous = Deno.env.get("CODEX_SESSION_LOG_USER_EMAIL_MAP");
  Deno.env.delete("CODEX_SESSION_LOG_USER_EMAIL_MAP");
  try {
    const first = await userIdForClientIdentity({
      installation_id: "install-123",
      local_username: "arya",
      hostname: "arya-mbp",
    });
    const repeat = await userIdForClientIdentity({
      installation_id: "install-123",
      local_username: "arya",
      hostname: "arya-mbp",
    });
    const other = await userIdForClientIdentity({
      installation_id: "install-456",
      local_username: "arya",
      hostname: "arya-mbp",
    });

    assertUuid(first);
    assertEquals(first, repeat);
    assertNotEquals(first, other);
    assertEquals(
      clientIdentityKey({
        installation_id: "install-123",
        local_username: "arya",
        hostname: "arya-mbp",
      }),
      "installation:install-123",
    );
  } finally {
    if (previous === undefined) {
      Deno.env.delete("CODEX_SESSION_LOG_USER_EMAIL_MAP");
    } else {
      Deno.env.set("CODEX_SESSION_LOG_USER_EMAIL_MAP", previous);
    }
  }
});

function assertEquals(actual: unknown, expected: unknown): void {
  if (actual !== expected) {
    throw new Error(`Expected ${expected}, got ${actual}`);
  }
}

function assertNotEquals(actual: unknown, expected: unknown): void {
  if (actual === expected) {
    throw new Error(`Expected ${actual} not to equal ${expected}`);
  }
}

function assertUuid(value: string): void {
  if (
    !/^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i
      .test(value)
  ) {
    throw new Error(`Expected UUID, got ${value}`);
  }
}
