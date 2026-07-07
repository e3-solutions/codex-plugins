type JsonObject = Record<string, unknown>;

const DNS_NAMESPACE_UUID_BYTES = new Uint8Array([
  0x6b,
  0xa7,
  0xb8,
  0x10,
  0x9d,
  0xad,
  0x11,
  0xd1,
  0x80,
  0xb4,
  0x00,
  0xc0,
  0x4f,
  0xd4,
  0x30,
  0xc8,
]);

export async function userIdForClientIdentity(
  client: JsonObject,
): Promise<string> {
  const gitEmail = normalizedOptionalString(client.git_email);
  if (gitEmail) {
    return userIdForEmail(gitEmail) ?? await deterministicUserIdForEmail(
      gitEmail,
    );
  }
  return deterministicUserIdForIdentity(clientIdentityKey(client));
}

export function clientIdentityKey(client: JsonObject): string {
  const explicit = normalizedOptionalString(client.identity_key);
  if (explicit) {
    return explicit;
  }

  const installationId = normalizedOptionalString(client.installation_id);
  if (installationId) {
    return `installation:${installationId}`;
  }

  const username = normalizedOptionalString(client.local_username);
  const hostname = normalizedOptionalString(client.hostname);
  if (username && hostname) {
    return `local:${username}@${hostname}`;
  }
  if (username) {
    return `local_username:${username}`;
  }
  if (hostname) {
    return `hostname:${hostname}`;
  }
  throw new Error("client.git_email or fallback identity is required");
}

function userIdForEmail(email: string): string | null {
  const raw = Deno.env.get("CODEX_SESSION_LOG_USER_EMAIL_MAP") ?? "{}";
  const map = JSON.parse(raw) as Record<string, unknown>;
  const mapped = map[email] ?? map[email.toLowerCase()];
  return typeof mapped === "string" && mapped.length > 0 ? mapped : null;
}

async function deterministicUserIdForEmail(email: string): Promise<string> {
  return uuidV5(
    `codex-session-logging:${email.trim().toLowerCase()}`,
    DNS_NAMESPACE_UUID_BYTES,
  );
}

async function deterministicUserIdForIdentity(key: string): Promise<string> {
  return uuidV5(`codex-session-logging:${key}`, DNS_NAMESPACE_UUID_BYTES);
}

async function uuidV5(name: string, namespace: Uint8Array): Promise<string> {
  const nameBytes = new TextEncoder().encode(name);
  const bytes = new Uint8Array(namespace.length + nameBytes.length);
  bytes.set(namespace);
  bytes.set(nameBytes, namespace.length);

  const digest = new Uint8Array(await crypto.subtle.digest("SHA-1", bytes));
  const uuid = digest.slice(0, 16);
  uuid[6] = (uuid[6] & 0x0f) | 0x50;
  uuid[8] = (uuid[8] & 0x3f) | 0x80;
  return formatUuid(uuid);
}

function formatUuid(bytes: Uint8Array): string {
  const hex = Array.from(bytes).map((byte) =>
    byte.toString(16).padStart(2, "0")
  ).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${
    hex.slice(16, 20)
  }-${hex.slice(20)}`;
}

function normalizedOptionalString(value: unknown): string | null {
  return typeof value === "string" && value.trim().length > 0
    ? value.trim().toLowerCase()
    : null;
}
