type JsonObject = Record<string, unknown>;

const DEFAULT_BUCKET = "codex-sessions";
const DEFAULT_ALLOWED_ORG = "e3-solutions";
const DNS_NAMESPACE_UUID_BYTES = new Uint8Array([
  0x6b, 0xa7, 0xb8, 0x10,
  0x9d, 0xad,
  0x11, 0xd1,
  0x80, 0xb4,
  0x00, 0xc0, 0x4f, 0xd4, 0x30, 0xc8,
]);

const corsHeaders = {
  "access-control-allow-origin": "*",
  "access-control-allow-headers": "authorization, x-client-info, apikey, content-type, x-codex-session-log-token",
  "access-control-allow-methods": "POST, OPTIONS",
};

Deno.serve(async (req) => {
  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: corsHeaders });
  }
  if (req.method !== "POST") {
    return jsonResponse({ error: "method_not_allowed" }, 405);
  }

  try {
    const tokenError = optionalIngestTokenError(req);
    if (tokenError) {
      return tokenError;
    }

    const body = await req.json();
    const payload = requireObject(body, "payload");
    const record = requireObject(payload.record, "record");
    const message = requireObject(payload.message, "message");
    const client = requireObject(payload.client, "client");

    const remote = requireString(client.repo_remote, "client.repo_remote");
    if (!remoteBelongsToOrg(remote, allowedGithubOrg())) {
      return jsonResponse({ error: "repo_not_allowed" }, 403);
    }

    const gitEmail = requireString(client.git_email, "client.git_email").trim().toLowerCase();
    const userId = userIdForEmail(gitEmail) ?? await deterministicUserIdForEmail(gitEmail);

    await validateMessageIntegrity(record, message);

    const storagePath = storagePathForUser(record, userId);
    await uploadStorageObject(storagePath, message);
    await upsertSession(record, client, userId, remote);
    await upsertMessage(record, userId, storagePath);

    return jsonResponse({
      ok: true,
      id: record.id,
      storage_path: storagePath,
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    return jsonResponse({ error: "ingest_failed", message }, 500);
  }
});

function optionalIngestTokenError(req: Request): Response | null {
  const expected = Deno.env.get("CODEX_SESSION_LOG_INGEST_TOKEN");
  if (!expected) {
    return null;
  }
  if (req.headers.get("x-codex-session-log-token") !== expected) {
    return jsonResponse({ error: "invalid_ingest_token" }, 401);
  }
  return null;
}

function userIdForEmail(email: string): string | null {
  const raw = Deno.env.get("CODEX_SESSION_LOG_USER_EMAIL_MAP") ?? "{}";
  const map = JSON.parse(raw) as Record<string, string>;
  return map[email] ?? map[email.toLowerCase()] ?? null;
}

async function deterministicUserIdForEmail(email: string): Promise<string> {
  const normalizedEmail = email.trim().toLowerCase();
  return uuidV5(`codex-session-logging:${normalizedEmail}`, DNS_NAMESPACE_UUID_BYTES);
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
  const hex = Array.from(bytes).map((byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function allowedGithubOrg(): string {
  return Deno.env.get("CODEX_SESSION_LOG_ALLOWED_GITHUB_ORG") ?? DEFAULT_ALLOWED_ORG;
}

function remoteBelongsToOrg(remote: string, org: string): boolean {
  const escapedOrg = escapeRegExp(org);
  return [
    new RegExp(`^https://github\\.com/${escapedOrg}/[^/]+(?:\\.git)?/?$`, "i"),
    new RegExp(`^git@github\\.com:${escapedOrg}/[^/]+(?:\\.git)?$`, "i"),
    new RegExp(`^ssh://git@github\\.com/${escapedOrg}/[^/]+(?:\\.git)?$`, "i"),
  ].some((pattern) => pattern.test(remote.trim()));
}

function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

async function validateMessageIntegrity(record: JsonObject, message: JsonObject): Promise<void> {
  const content = requireString(message.content, "message.content");
  const expectedHash = requireString(record.content_sha256, "record.content_sha256");
  const actualHash = await sha256Hex(content);
  if (actualHash !== expectedHash) {
    throw new Error("content hash mismatch");
  }

  const expectedByteSize = requireNumber(record.content_byte_size, "record.content_byte_size");
  const actualByteSize = new TextEncoder().encode(content).byteLength;
  if (actualByteSize !== expectedByteSize) {
    throw new Error("content byte size mismatch");
  }
}

async function sha256Hex(value: string): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value));
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

function storagePathForUser(record: JsonObject, userId: string): string {
  const sessionId = safeSegment(requireString(record.session_id, "record.session_id"));
  const role = safeSegment(requireString(record.role, "record.role"));
  const seq = requireNumber(record.seq, "record.seq");
  return `users/${safeSegment(userId)}/sessions/${sessionId}/messages/${String(seq).padStart(6, "0")}-${role}.json`;
}

async function uploadStorageObject(storagePath: string, message: JsonObject): Promise<void> {
  const bucket = Deno.env.get("CODEX_SESSION_LOG_BUCKET") ?? DEFAULT_BUCKET;
  const quotedPath = storagePath.split("/").map(encodeURIComponent).join("/");
  await supabaseFetch(
    `/storage/v1/object/${encodeURIComponent(bucket)}/${quotedPath}`,
    {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "x-upsert": "true",
      },
      body: JSON.stringify(message, null, 2) + "\n",
    },
  );
}

async function upsertSession(record: JsonObject, client: JsonObject, userId: string, remote: string): Promise<void> {
  const sessionId = requireString(record.session_id, "record.session_id");
  const row = {
    id: sessionId,
    user_id: userId,
    repo: remote,
    branch: optionalString(client.git_branch),
    storage_prefix: `users/${safeSegment(userId)}/sessions/${safeSegment(sessionId)}`,
    metadata: {
      ...optionalObject(record.metadata),
      client,
    },
    updated_at: new Date().toISOString(),
  };
  await restUpsert("codex_sessions", row, "id");
}

async function upsertMessage(record: JsonObject, userId: string, storagePath: string): Promise<void> {
  const row = {
    id: requireString(record.id, "record.id"),
    session_id: requireString(record.session_id, "record.session_id"),
    user_id: userId,
    turn_id: optionalString(record.turn_id),
    seq: requireNumber(record.seq, "record.seq"),
    role: requireString(record.role, "record.role"),
    storage_bucket: Deno.env.get("CODEX_SESSION_LOG_BUCKET") ?? DEFAULT_BUCKET,
    storage_path: storagePath,
    content_sha256: requireString(record.content_sha256, "record.content_sha256"),
    content_byte_size: requireNumber(record.content_byte_size, "record.content_byte_size"),
    content_excerpt: optionalString(record.content_excerpt),
    metadata: optionalObject(record.metadata),
    created_at: requireString(record.created_at, "record.created_at"),
  };
  await restUpsert("codex_session_messages", row, "id");
}

async function restUpsert(table: string, row: JsonObject, conflict: string): Promise<void> {
  await supabaseFetch(
    `/rest/v1/${table}?on_conflict=${encodeURIComponent(conflict)}`,
    {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "prefer": "resolution=merge-duplicates,return=minimal",
      },
      body: JSON.stringify(row),
    },
  );
}

async function supabaseFetch(path: string, init: RequestInit): Promise<void> {
  const key = supabaseSecretKey();
  const response = await fetch(`${supabaseUrl()}${path}`, {
    ...init,
    headers: {
      apikey: key,
      ...legacyJwtKeyAuthHeader(key),
      ...(init.headers ?? {}),
    },
  });
  if (!response.ok) {
    throw new Error(`Supabase request failed ${response.status}: ${await response.text()}`);
  }
}

function legacyJwtKeyAuthHeader(key: string): Record<string, string> {
  if (!key.startsWith("eyJ")) {
    return {};
  }
  return { authorization: `Bearer ${key}` };
}

function supabaseUrl(): string {
  const value = Deno.env.get("SUPABASE_URL");
  if (!value) {
    throw new Error("SUPABASE_URL is required");
  }
  return value.replace(/\/+$/, "");
}

function supabaseSecretKey(): string {
  const secretKeys = Deno.env.get("SUPABASE_SECRET_KEYS");
  if (secretKeys) {
    const parsed = JSON.parse(secretKeys) as Record<string, string>;
    if (parsed.default) {
      return parsed.default;
    }
  }

  const legacyServiceRole = Deno.env.get("SUPABASE_SERVICE_ROLE_KEY");
  if (legacyServiceRole) {
    return legacyServiceRole;
  }

  throw new Error("SUPABASE_SECRET_KEYS or SUPABASE_SERVICE_ROLE_KEY is required");
}

function jsonResponse(payload: JsonObject, status = 200): Response {
  return new Response(JSON.stringify(payload), {
    status,
    headers: {
      ...corsHeaders,
      "content-type": "application/json",
    },
  });
}

function requireObject(value: unknown, name: string): JsonObject {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error(`${name} must be an object`);
  }
  return value as JsonObject;
}

function optionalObject(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value) ? value as JsonObject : {};
}

function requireString(value: unknown, name: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new Error(`${name} must be a non-empty string`);
  }
  return value;
}

function optionalString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function requireNumber(value: unknown, name: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new Error(`${name} must be a finite number`);
  }
  return value;
}

function safeSegment(value: string): string {
  const cleaned = value
    .trim()
    .replace(/[^a-zA-Z0-9._-]+/g, "-")
    .split("-")
    .filter(Boolean)
    .join("-");
  return cleaned || "unknown";
}
