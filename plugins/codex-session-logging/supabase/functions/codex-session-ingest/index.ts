import { userIdForClientIdentity } from "./client_identity.ts";
import { sanitizeEventPayload } from "./event_sanitizer.ts";

type JsonObject = Record<string, unknown>;
type ExistingSession = {
  found: boolean;
  installationCapabilitySha256: string | null;
  metadata: JsonObject;
  startedAt: string | null;
  threadId: string | null;
  userId: string | null;
};

class PayloadValidationError extends Error {}

const DEFAULT_BUCKET = "codex-sessions";
const MAX_ROLLOUT_CHUNK_BYTES = 1024 * 1024;
const MAX_ROLLOUT_CHUNK_BASE64_LENGTH = Math.ceil(MAX_ROLLOUT_CHUNK_BYTES / 3) *
  4;
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const COMPACT_UUID_PATTERN = /^[0-9a-f]{32}$/;
const UUID_V4_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const HEX_GENERATION_PATTERN = /^[0-9a-f]{16,64}$/;
const SHA256_PATTERN = /^[0-9a-f]{64}$/;
const SAFE_CATEGORY_PATTERN = /^[a-zA-Z0-9._-]{1,128}$/;
const BASE64_PATTERN =
  /^(?:[A-Za-z0-9+/]{4})*(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$/;

const corsHeaders = {
  "access-control-allow-origin": "*",
  "access-control-allow-headers":
    "authorization, x-client-info, apikey, content-type, x-codex-session-log-token",
  "access-control-allow-methods": "POST, OPTIONS",
};

export async function handleRequest(req: Request): Promise<Response> {
  if (req.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: corsHeaders });
  }
  if (req.method !== "POST") {
    return jsonResponse({ error: "method_not_allowed" }, 405);
  }

  try {
    const tokenError = ingestTokenError(req);
    if (tokenError) {
      return tokenError;
    }

    const body = await requestJson(req);
    const payload = requireObject(body, "payload");
    const client = requireObject(payload.client, "client");

    const remote = requireString(client.repo_remote, "client.repo_remote");

    const payloadKind = optionalString(payload.kind);
    if (payloadKind !== "rollout_chunk" && isHistoricalBackfill(payload)) {
      return jsonResponse({
        ok: true,
        ignored: true,
        reason: "historical_backfill_disabled",
      });
    }

    if (payloadKind === "rollout_chunk") {
      return await ingestRolloutChunk(payload, client, remote);
    }

    const record = requireObject(payload.record, "record");
    const sessionId = requireString(record.session_id, "record.session_id");
    const recordType = optionalString(record.type) ?? "message";
    if (await ignoredSession(sessionId)) {
      if (recordType !== "usage") {
        const ignoredUserId = await resolveUserId(client);
        await deleteStorageObject(storagePathForRecord(record, ignoredUserId));
      }
      return ignoredSessionResponse(record);
    }
    if (recordType === "usage") {
      const usage = requireObject(payload.usage, "usage");
      const usageParameters = sessionUsageParameters(record, usage);
      if (!await usageCapabilityMatches(record, client)) {
        return jsonResponse({ error: "usage_ingest_retryable" }, 503);
      }
      await upsertSessionUsage(usageParameters);
      return jsonResponse({
        ok: true,
        id: record.id,
        kind: "usage",
      });
    }
    let message: JsonObject | null = null;
    let messageRequestsOptOut = false;
    if (recordType === "message") {
      message = requireObject(payload.message, "message");
      validateCanonicalMessageRecord(record);
      await validateMessageIntegrity(record, message);
      messageRequestsOptOut = messageOptsOut(
        requireString(message.content, "message.content"),
      );
      if (
        messageRequestsOptOut && optionalString(record.type) !== "message"
      ) {
        throw new PayloadValidationError(
          "opt-out requires an explicit message record",
        );
      }
    } else if (recordType !== "event") {
      throw new PayloadValidationError(
        "record.type must be message, event, or usage",
      );
    }
    const userId = await resolveUserId(client);
    const existing = await existingSession(
      requireString(record.session_id, "record.session_id"),
    );
    if (existing.found && existing.userId !== userId) {
      return jsonResponse({ error: "session_rejected" }, 422);
    }
    if (messageRequestsOptOut) {
      await fenceOwnedCodexSession(sessionId, userId);
      return ignoredSessionResponse(record);
    }
    const storagePath = storagePathForRecord(record, userId);
    if (!await reserveSessionStorage(sessionId, userId)) {
      await deleteStorageObject(storagePath);
      return ignoredSessionResponse(record);
    }

    if (recordType === "event") {
      const event = requireObject(payload.event, "event");
      const sanitizedEvent = sanitizeEventPayload(record, event);
      await upsertSessionUser(record, client, userId);
      await uploadStorageObject(storagePath, sanitizedEvent);
      if (
        await finishSessionObjectWrite(record, storagePath, async () => {
          await upsertSession(
            record,
            client,
            userId,
            remote,
            existing,
            optionalObject(sanitizedEvent.metadata),
          );
          await upsertEvent(record, userId, storagePath, sanitizedEvent);
        })
      ) {
        return ignoredSessionResponse(record);
      }
      return jsonResponse({
        ok: true,
        id: record.id,
        storage_path: storagePath,
      });
    }

    if (!message) {
      throw new PayloadValidationError("message must be an object");
    }
    await upsertSessionUser(record, client, userId);
    await uploadStorageObject(storagePath, message);
    if (
      await finishSessionObjectWrite(record, storagePath, async () => {
        await upsertSession(record, client, userId, remote, existing);
        await upsertMessage(record, userId, storagePath);
      })
    ) {
      return ignoredSessionResponse(record);
    }

    return jsonResponse({
      ok: true,
      id: record.id,
      storage_path: storagePath,
    });
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    if (error instanceof PayloadValidationError) {
      return jsonResponse({ error: "invalid_payload", message }, 400);
    }
    return jsonResponse({ error: "ingest_failed", message }, 500);
  }
}

export function messageOptsOut(content: string): boolean {
  return content.includes("--ignore-extension");
}

async function ingestRolloutChunk(
  payload: JsonObject,
  client: JsonObject,
  remote: string,
): Promise<Response> {
  const record = requireObject(payload.record, "record");
  if (requireString(record.type, "record.type") !== "event") {
    throw new PayloadValidationError("record.type must be event");
  }
  if (
    requireString(record.event_type, "record.event_type") !== "rollout_chunk"
  ) {
    throw new PayloadValidationError("record.event_type must be rollout_chunk");
  }

  const sessionId = requireCanonicalUuid(
    record.session_id,
    "record.session_id",
  );
  const chunk = requireObject(payload.rollout_chunk, "rollout_chunk");
  const fileGeneration = requirePatternString(
    chunk.file_generation,
    "rollout_chunk.file_generation",
    HEX_GENERATION_PATTERN,
    "lowercase hexadecimal",
  );
  const startOffset = requireNonNegativeInteger(
    chunk.start_offset,
    "rollout_chunk.start_offset",
  );
  const endOffset = requireNonNegativeInteger(
    chunk.end_offset,
    "rollout_chunk.end_offset",
  );
  if (endOffset <= startOffset) {
    throw new PayloadValidationError(
      "rollout_chunk.end_offset must be greater than start_offset",
    );
  }
  const contentByteSize = requireNonNegativeInteger(
    chunk.content_byte_size,
    "rollout_chunk.content_byte_size",
  );
  if (contentByteSize !== endOffset - startOffset) {
    throw new PayloadValidationError(
      "rollout_chunk byte size must equal end_offset - start_offset",
    );
  }
  if (contentByteSize > MAX_ROLLOUT_CHUNK_BYTES) {
    throw new PayloadValidationError(
      `rollout_chunk.content_byte_size must not exceed ${MAX_ROLLOUT_CHUNK_BYTES}`,
    );
  }
  const contentSha256 = requirePatternString(
    chunk.content_sha256,
    "rollout_chunk.content_sha256",
    SHA256_PATTERN,
    "a lowercase SHA-256 hex digest",
  );
  const contentBase64 = requirePatternString(
    chunk.content_base64,
    "rollout_chunk.content_base64",
    BASE64_PATTERN,
    "canonical base64",
  );
  if (contentBase64.length > MAX_ROLLOUT_CHUNK_BASE64_LENGTH) {
    throw new PayloadValidationError(
      "rollout_chunk.content_base64 exceeds the encoded size limit",
    );
  }
  const content = decodeBase64(contentBase64);
  if (content.byteLength !== contentByteSize) {
    throw new PayloadValidationError(
      "rollout chunk content byte size mismatch",
    );
  }
  if (await sha256HexBytes(content) !== contentSha256) {
    throw new PayloadValidationError("rollout chunk content hash mismatch");
  }
  const userId = await resolveUserId(client);
  const storagePath = rolloutStoragePath(
    userId,
    sessionId,
    fileGeneration,
    startOffset,
    endOffset,
    contentSha256,
  );
  if (await ignoredSession(sessionId)) {
    await deleteStorageObject(storagePath);
    return ignoredSessionResponse(record);
  }
  const existing = await existingSession(sessionId);
  if (existing.found && existing.userId !== userId) {
    return jsonResponse({ error: "session_rejected" }, 422);
  }
  if (!await reserveSessionStorage(sessionId, userId)) {
    await deleteStorageObject(storagePath);
    return ignoredSessionResponse(record);
  }

  const metadata = sanitizeRolloutChunkMetadata(
    optionalObject(record.metadata),
    {
      file_generation: fileGeneration,
      start_offset: startOffset,
      end_offset: endOffset,
      content_byte_size: contentByteSize,
      content_sha256: contentSha256,
    },
  );
  const eventId = await deterministicUuid(
    `rollout-chunk-v1:${sessionId}:${fileGeneration}:${startOffset}:${endOffset}:${contentSha256}`,
  );
  const catalogRecord: JsonObject = {
    ...record,
    id: eventId,
    session_id: sessionId,
    type: "event",
    event_type: "rollout_chunk",
    seq: deterministicEventSequence(eventId),
    metadata,
  };
  const event = { metadata };

  await upsertSessionUser(catalogRecord, client, userId);
  await uploadStorageBytes(
    storagePath,
    exactArrayBuffer(content),
    "application/x-ndjson",
  );
  if (
    await finishSessionObjectWrite(
      catalogRecord,
      storagePath,
      async () => {
        await upsertSession(
          catalogRecord,
          client,
          userId,
          remote,
          existing,
          metadata,
        );
        await upsertEvent(catalogRecord, userId, storagePath, event);
      },
    )
  ) {
    return ignoredSessionResponse(catalogRecord);
  }
  return jsonResponse({
    ok: true,
    id: eventId,
    kind: "rollout_chunk",
    storage_path: storagePath,
  });
}

function isHistoricalBackfill(payload: JsonObject): boolean {
  if (optionalString(payload.kind) === "backfill_status") {
    return true;
  }
  const record = optionalObject(payload.record);
  const metadata = optionalObject(record.metadata);
  return optionalString(metadata.source) === "historical_transcript" ||
    optionalString(record.hook_event_name) === "HistoricalBackfill";
}

if (import.meta.main) {
  Deno.serve(handleRequest);
}

async function requestJson(req: Request): Promise<unknown> {
  try {
    return await req.json();
  } catch {
    throw new PayloadValidationError("request body must be valid JSON");
  }
}

function ingestTokenError(req: Request): Response | null {
  const expected = Deno.env.get("CODEX_SESSION_LOG_INGEST_TOKEN");
  if (!expected) {
    return null;
  }
  if (req.headers.get("x-codex-session-log-token") !== expected) {
    return jsonResponse({ error: "invalid_ingest_token" }, 401);
  }
  return null;
}

async function validateMessageIntegrity(
  record: JsonObject,
  message: JsonObject,
): Promise<void> {
  const content = requireString(message.content, "message.content");
  const expectedHash = requireString(
    record.content_sha256,
    "record.content_sha256",
  );
  const actualHash = await sha256Hex(content);
  if (actualHash !== expectedHash) {
    throw new PayloadValidationError("content hash mismatch");
  }

  const expectedByteSize = requireNumber(
    record.content_byte_size,
    "record.content_byte_size",
  );
  const actualByteSize = new TextEncoder().encode(content).byteLength;
  if (actualByteSize !== expectedByteSize) {
    throw new PayloadValidationError("content byte size mismatch");
  }
}

function validateCanonicalMessageRecord(record: JsonObject): void {
  requireDatabaseUuid(record.id, "record.id");
  requireString(record.session_id, "record.session_id");
  const seq = requireNumber(record.seq, "record.seq");
  if (!Number.isSafeInteger(seq) || seq < 0) {
    throw new PayloadValidationError(
      "record.seq must be a non-negative integer",
    );
  }
  const role = requireString(record.role, "record.role");
  if (role !== "user" && role !== "assistant") {
    throw new PayloadValidationError("record.role must be user or assistant");
  }
  const createdAt = requireString(record.created_at, "record.created_at");
  if (Number.isNaN(Date.parse(createdAt))) {
    throw new PayloadValidationError("record.created_at must be a timestamp");
  }
}

async function sha256Hex(value: string): Promise<string> {
  return await sha256HexBytes(new TextEncoder().encode(value));
}

async function sha256HexBytes(value: Uint8Array): Promise<string> {
  const digest = await crypto.subtle.digest("SHA-256", exactArrayBuffer(value));
  return Array.from(new Uint8Array(digest))
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
}

function exactArrayBuffer(value: Uint8Array): ArrayBuffer {
  return value.buffer.slice(
    value.byteOffset,
    value.byteOffset + value.byteLength,
  ) as ArrayBuffer;
}

async function deterministicUuid(value: string): Promise<string> {
  const digest = new Uint8Array(
    await crypto.subtle.digest("SHA-256", new TextEncoder().encode(value)),
  ).slice(0, 16);
  digest[6] = (digest[6] & 0x0f) | 0x50;
  digest[8] = (digest[8] & 0x3f) | 0x80;
  const hex = Array.from(digest).map((byte) =>
    byte.toString(16).padStart(2, "0")
  ).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${
    hex.slice(16, 20)
  }-${hex.slice(20)}`;
}

function deterministicEventSequence(eventId: string): number {
  const prefix = Number.parseInt(eventId.replaceAll("-", "").slice(0, 8), 16);
  return -(prefix % 2_000_000_000 + 1);
}

function decodeBase64(value: string): Uint8Array {
  try {
    const decoded = atob(value);
    if (btoa(decoded) !== value) {
      throw new Error("non-canonical base64");
    }
    return Uint8Array.from(decoded, (character) => character.charCodeAt(0));
  } catch {
    throw new PayloadValidationError(
      "rollout_chunk.content_base64 must be valid base64",
    );
  }
}

function sanitizeRolloutChunkMetadata(
  source: JsonObject,
  chunkMetadata: JsonObject,
): JsonObject {
  const metadata: JsonObject = {
    source: "rollout_sync",
    ...chunkMetadata,
  };
  for (const field of ["cwd", "transcript_path"]) {
    const value = optionalBoundedString(source[field], field, 4096);
    if (value) {
      metadata[field] = value;
    }
  }
  const threadSource = optionalString(source.thread_source);
  if (threadSource) {
    if (!SAFE_CATEGORY_PATTERN.test(threadSource)) {
      throw new PayloadValidationError(
        "record.metadata.thread_source is invalid",
      );
    }
    metadata.thread_source = threadSource;
  }
  const rolloutSourceCategory = optionalBoundedString(
    source.rollout_source_category,
    "rollout_source_category",
    128,
  );
  if (rolloutSourceCategory) {
    if (!SAFE_CATEGORY_PATTERN.test(rolloutSourceCategory)) {
      throw new PayloadValidationError(
        "record.metadata.rollout_source_category is invalid",
      );
    }
    metadata.rollout_source_category = rolloutSourceCategory;
  }
  for (const field of ["parent_thread_id", "root_thread_id"]) {
    if (source[field] !== undefined && source[field] !== null) {
      metadata[field] = requireCanonicalUuid(
        source[field],
        `record.metadata.${field}`,
      );
    }
  }
  return metadata;
}

function rolloutStoragePath(
  userId: string,
  sessionId: string,
  fileGeneration: string,
  startOffset: number,
  endOffset: number,
  contentSha256: string,
): string {
  return `users/${
    safeSegment(userId)
  }/sessions/${sessionId}/rollouts/${fileGeneration}/${startOffset}-${endOffset}-${contentSha256}.jsonl`;
}

function storagePathForRecord(record: JsonObject, userId: string): string {
  const sessionId = safeSegment(
    requireString(record.session_id, "record.session_id"),
  );
  const seq = requireNumber(record.seq, "record.seq");
  if (optionalString(record.type) === "event") {
    const eventType = safeSegment(
      requireString(record.event_type, "record.event_type"),
    );
    return `users/${safeSegment(userId)}/sessions/${sessionId}/events/${
      String(seq).padStart(6, "0")
    }-${eventType}.json`;
  }
  const role = safeSegment(requireString(record.role, "record.role"));
  return `users/${safeSegment(userId)}/sessions/${sessionId}/messages/${
    String(seq).padStart(6, "0")
  }-${role}.json`;
}

async function uploadStorageObject(
  storagePath: string,
  payload: JsonObject,
): Promise<void> {
  await uploadStorageBytes(
    storagePath,
    JSON.stringify(payload, null, 2) + "\n",
    "application/json",
  );
}

async function uploadStorageBytes(
  storagePath: string,
  payload: BodyInit,
  contentType: string,
): Promise<void> {
  const bucket = storageBucket();
  const quotedPath = storagePath.split("/").map(encodeURIComponent).join("/");
  await supabaseFetch(
    `/storage/v1/object/${encodeURIComponent(bucket)}/${quotedPath}`,
    {
      method: "POST",
      headers: {
        "content-type": contentType,
        "x-upsert": "true",
      },
      body: payload,
    },
  );
}

async function deleteStorageObject(storagePath: string): Promise<void> {
  const bucket = storageBucket();
  await supabaseFetch(
    `/storage/v1/object/${encodeURIComponent(bucket)}`,
    {
      method: "DELETE",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({ prefixes: [storagePath] }),
    },
  );
}

async function finishSessionObjectWrite(
  record: JsonObject,
  storagePath: string,
  writeCatalog: () => Promise<void>,
): Promise<boolean> {
  const sessionId = requireString(record.session_id, "record.session_id");
  try {
    await writeCatalog();
  } catch (error) {
    if (!await ignoredSession(sessionId)) {
      throw error;
    }
    await deleteStorageObject(storagePath);
    return true;
  }
  if (!await ignoredSession(sessionId)) {
    return false;
  }
  await deleteStorageObject(storagePath);
  return true;
}

async function upsertSession(
  record: JsonObject,
  client: JsonObject,
  userId: string,
  remote: string,
  existing: ExistingSession,
  metadata = optionalObject(record.metadata),
): Promise<void> {
  const sessionId = requireString(record.session_id, "record.session_id");
  const threadId = existing.threadId ??
    optionalString(record.thread_id) ?? await sha256Hex(sessionId);
  const installationCapabilitySha256 = existing.found
    ? existing.installationCapabilitySha256
    : await installationCapabilityDigest(client.installation_id);
  const sessionMetadata = await sessionMetadataForUpsert(
    metadata,
    client,
    existing.metadata,
  );
  const row = {
    id: sessionId,
    thread_id: threadId,
    installation_capability_sha256: installationCapabilitySha256,
    user_id: userId,
    repo: remote,
    branch: optionalString(client.git_branch),
    storage_prefix: sessionStoragePrefix(userId, sessionId),
    metadata: sessionMetadata,
    started_at: earliestTimestamp(
      existing.startedAt,
      requireString(record.created_at, "record.created_at"),
    ),
    // Persist an explicit session end when the client sends one (Claude Stop /
    // SessionEnd, or an idle-timeout presence tick). Absent on ordinary events,
    // in which case we clear ended_at so a resumed session lights up again.
    // Codex never sends ended_at, so this stays null for Codex — behavior
    // unchanged.
    ended_at: optionalString(record.ended_at),
    updated_at: new Date().toISOString(),
  };
  await restUpsert("codex_sessions", row, "id");
}

async function upsertSessionUser(
  record: JsonObject,
  client: JsonObject,
  userId: string,
): Promise<void> {
  const observedAt = requireString(record.created_at, "record.created_at");
  const row: JsonObject = {
    user_id: userId,
    first_seen_at: observedAt,
    last_seen_at: observedAt,
  };
  for (
    const [sourceKey, column] of [
      ["git_email", "git_email"],
      ["git_user_name", "git_user_name"],
      ["linear_user_name", "linear_user_name"],
      ["local_username", "local_username"],
      ["hostname", "hostname"],
      ["installation_id", "installation_id"],
    ]
  ) {
    const value = optionalString(client[sourceKey]);
    if (value) {
      row[column] = value;
    }
  }
  await restUpsert("codex_session_users", row, "user_id");
}

async function resolveUserId(client: JsonObject): Promise<string> {
  const installationId = optionalString(client.installation_id);
  if (!installationId) {
    return userIdForClientIdentity(client);
  }

  const response = await supabaseFetch(
    `/rest/v1/codex_session_users?select=user_id,git_email,first_seen_at&installation_id=eq.${
      encodeURIComponent(installationId)
    }`,
    {
      method: "GET",
      headers: {
        accept: "application/json",
      },
    },
  );
  const rows = await response.json();
  if (!Array.isArray(rows)) {
    throw new Error("invalid codex_session_users response");
  }

  const currentEmail = optionalString(client.git_email)?.toLowerCase();
  const candidates = rows.map(optionalObject).filter((row) =>
    optionalString(row.user_id) !== null
  );
  candidates.sort((left, right) => {
    const leftEmail = optionalString(left.git_email)?.toLowerCase();
    const rightEmail = optionalString(right.git_email)?.toLowerCase();
    const leftRank = leftEmail === currentEmail && currentEmail
      ? 0
      : leftEmail
      ? 1
      : 2;
    const rightRank = rightEmail === currentEmail && currentEmail
      ? 0
      : rightEmail
      ? 1
      : 2;
    if (leftRank !== rightRank) {
      return leftRank - rightRank;
    }
    const seenComparison = (optionalString(left.first_seen_at) ?? "")
      .localeCompare(optionalString(right.first_seen_at) ?? "");
    if (seenComparison !== 0) {
      return seenComparison;
    }
    return requireString(left.user_id, "user_id").localeCompare(
      requireString(right.user_id, "user_id"),
    );
  });
  const existingUserId = candidates.length > 0
    ? optionalString(candidates[0].user_id)
    : null;
  return existingUserId ?? await userIdForClientIdentity(client);
}

async function sessionMetadataForUpsert(
  metadata: JsonObject,
  client: JsonObject,
  existingMetadata: JsonObject,
): Promise<JsonObject> {
  const nextMetadata: JsonObject = { ...existingMetadata, ...metadata };
  // Stamp the coding-agent family onto the session row so downstream consumers
  // (heartbeat dashboard, codestat) can label sessions without heuristics.
  // Sticky: once a session is seen as "claude" it stays "claude" even if a later
  // event omits the tag. Defaults to "codex" to preserve existing rows.
  nextMetadata.agent = optionalString(metadata.agent) ??
    optionalString(existingMetadata.agent) ?? "codex";
  return {
    ...nextMetadata,
    client: {
      ...optionalObject(existingMetadata.client),
      ...client,
    },
  };
}

async function existingSession(
  sessionId: string,
): Promise<ExistingSession> {
  const response = await supabaseFetch(
    `/rest/v1/codex_sessions?select=metadata,thread_id,started_at,installation_capability_sha256,user_id&id=eq.${
      encodeURIComponent(sessionId)
    }&limit=1`,
    {
      method: "GET",
      headers: {
        accept: "application/json",
      },
    },
  );
  const rows = await response.json();
  if (!Array.isArray(rows) || rows.length === 0) {
    return {
      found: false,
      installationCapabilitySha256: null,
      metadata: {},
      threadId: null,
      startedAt: null,
      userId: null,
    };
  }
  const row = optionalObject(rows[0]);
  return {
    found: true,
    installationCapabilitySha256: optionalString(
      row.installation_capability_sha256,
    ),
    metadata: optionalObject(row.metadata),
    threadId: optionalString(row.thread_id),
    startedAt: optionalString(row.started_at),
    userId: optionalString(row.user_id),
  };
}

function storageBucket(): string {
  return Deno.env.get("CODEX_SESSION_LOG_BUCKET") ?? DEFAULT_BUCKET;
}

function sessionStoragePrefix(userId: string, sessionId: string): string {
  return `users/${safeSegment(userId)}/sessions/${safeSegment(sessionId)}`;
}

async function reserveSessionStorage(
  sessionId: string,
  userId: string,
): Promise<boolean> {
  const response = await supabaseFetch(
    "/rest/v1/rpc/reserve_codex_session_storage",
    {
      method: "POST",
      headers: { "content-type": "application/json" },
      body: JSON.stringify({
        p_session_id: sessionId,
        p_user_id: userId,
        p_storage_bucket: storageBucket(),
        p_storage_prefix: sessionStoragePrefix(userId, sessionId),
      }),
    },
  );
  const payload = await response.json();
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    throw new Error("session storage reservation returned an invalid response");
  }
  const status = optionalString((payload as JsonObject).status);
  if (status === "reserved") {
    return true;
  }
  if (status === "ignored") {
    return false;
  }
  throw new Error("session storage reservation returned an invalid status");
}

async function ignoredSession(sessionId: string): Promise<boolean> {
  const sessionHash = await sha256Hex(
    `codex_rollout_session:${sessionId}`,
  );
  const response = await supabaseFetch(
    `/rest/v1/codex_ignored_sessions?select=session_id_hash&session_id_hash=eq.${sessionHash}&limit=1`,
    {
      method: "GET",
      headers: { accept: "application/json" },
    },
  );
  const rows = await response.json();
  if (!Array.isArray(rows)) {
    throw new Error("ignored session lookup returned an invalid response");
  }
  return rows.length > 0;
}

async function fenceOwnedCodexSession(
  sessionId: string,
  userId: string,
): Promise<void> {
  const response = await supabaseFetch(
    "/rest/v1/rpc/fence_owned_codex_session",
    {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "prefer": "return=representation",
      },
      body: JSON.stringify({
        p_session_id: sessionId,
        p_user_id: userId,
        p_storage_bucket: storageBucket(),
        p_storage_prefix: sessionStoragePrefix(userId, sessionId),
      }),
    },
  );
  const payload = await response.json();
  if (
    !payload || typeof payload !== "object" || Array.isArray(payload) ||
    optionalString((payload as JsonObject).status) !== "fenced"
  ) {
    throw new Error("session fence returned an invalid response");
  }
}

function ignoredSessionResponse(record: JsonObject): Response {
  return jsonResponse({
    ok: true,
    id: record.id,
    ignored: true,
    reason: "session_opted_out",
  });
}

async function installationCapabilityDigest(
  value: unknown,
): Promise<string | null> {
  const installationId = optionalString(value);
  return installationId && UUID_V4_PATTERN.test(installationId)
    ? await sha256Hex(installationId)
    : null;
}

async function usageCapabilityMatches(
  record: JsonObject,
  client: JsonObject,
): Promise<boolean> {
  const incomingDigest = await installationCapabilityDigest(
    client.installation_id,
  );
  if (!incomingDigest) {
    return false;
  }
  const existing = await existingSession(
    requireString(record.session_id, "record.session_id"),
  );
  const storedDigest = existing.installationCapabilitySha256;
  if (!existing.found || !storedDigest || !SHA256_PATTERN.test(storedDigest)) {
    return false;
  }
  return constantTimeEqual(incomingDigest, storedDigest);
}

function constantTimeEqual(left: string, right: string): boolean {
  let difference = left.length ^ right.length;
  for (let index = 0; index < 64; index += 1) {
    difference |= (left.charCodeAt(index) || 0) ^
      (right.charCodeAt(index) || 0);
  }
  return difference === 0;
}

function earliestTimestamp(existing: string | null, incoming: string): string {
  if (!existing) {
    return incoming;
  }
  const existingTime = Date.parse(existing);
  const incomingTime = Date.parse(incoming);
  if (Number.isNaN(existingTime) || Number.isNaN(incomingTime)) {
    return existing;
  }
  return existingTime <= incomingTime ? existing : incoming;
}

async function upsertMessage(
  record: JsonObject,
  userId: string,
  storagePath: string,
): Promise<void> {
  const row = {
    id: requireString(record.id, "record.id"),
    session_id: requireString(record.session_id, "record.session_id"),
    user_id: userId,
    turn_id: optionalString(record.turn_id),
    seq: requireNumber(record.seq, "record.seq"),
    role: requireString(record.role, "record.role"),
    storage_bucket: storageBucket(),
    storage_path: storagePath,
    content_sha256: requireString(
      record.content_sha256,
      "record.content_sha256",
    ),
    content_byte_size: requireNumber(
      record.content_byte_size,
      "record.content_byte_size",
    ),
    content_excerpt: optionalString(record.content_excerpt),
    metadata: optionalObject(record.metadata),
    created_at: requireString(record.created_at, "record.created_at"),
  };
  await restUpsert("codex_session_messages", row, "id");
}

function sessionUsageParameters(
  record: JsonObject,
  usage: JsonObject,
): JsonObject {
  const modelContextWindow = optionalNonNegativeInteger(
    usage.model_context_window,
  );
  const inputTokens = requireNonNegativeInteger(
    usage.input_tokens,
    "usage.input_tokens",
  );
  const cachedInputTokens = requireNonNegativeInteger(
    usage.cached_input_tokens,
    "usage.cached_input_tokens",
  );
  const outputTokens = requireNonNegativeInteger(
    usage.output_tokens,
    "usage.output_tokens",
  );
  const reasoningOutputTokens = requireNonNegativeInteger(
    usage.reasoning_output_tokens,
    "usage.reasoning_output_tokens",
  );
  const totalTokens = requireNonNegativeInteger(
    usage.total_tokens,
    "usage.total_tokens",
  );
  const componentTotal = inputTokens + cachedInputTokens + outputTokens +
    reasoningOutputTokens;
  if (!Number.isSafeInteger(componentTotal) || componentTotal !== totalTokens) {
    throw new PayloadValidationError(
      "usage token components must sum exactly to usage.total_tokens",
    );
  }
  return {
    p_session_id: requireString(record.session_id, "record.session_id"),
    p_input_tokens: inputTokens,
    p_cached_input_tokens: cachedInputTokens,
    p_output_tokens: outputTokens,
    p_reasoning_output_tokens: reasoningOutputTokens,
    p_total_tokens: totalTokens,
    p_model_context_window: modelContextWindow,
    p_observed_at: requireString(usage.created_at, "usage.created_at"),
    p_metadata: optionalObject(usage.metadata),
  };
}

async function upsertSessionUsage(parameters: JsonObject): Promise<void> {
  await supabaseFetch(
    "/rest/v1/rpc/upsert_codex_session_usage_latest",
    {
      method: "POST",
      headers: {
        "content-type": "application/json",
        "prefer": "return=minimal",
      },
      body: JSON.stringify(parameters),
    },
  );
}

async function upsertEvent(
  record: JsonObject,
  userId: string,
  storagePath: string,
  event: JsonObject,
): Promise<void> {
  const row = {
    id: requireString(record.id, "record.id"),
    session_id: requireString(record.session_id, "record.session_id"),
    user_id: userId,
    seq: requireNumber(record.seq, "record.seq"),
    event_type: requireString(record.event_type, "record.event_type"),
    storage_bucket: storageBucket(),
    storage_path: storagePath,
    metadata: optionalObject(event.metadata),
    created_at: requireString(record.created_at, "record.created_at"),
  };
  await restUpsert("codex_session_events", row, "id");
}

async function restUpsert(
  table: string,
  row: JsonObject,
  conflict: string,
): Promise<void> {
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

async function supabaseFetch(
  path: string,
  init: RequestInit,
): Promise<Response> {
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
    throw new Error(
      `Supabase request failed ${response.status}: ${await response.text()}`,
    );
  }
  return response;
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

  throw new Error(
    "SUPABASE_SECRET_KEYS or SUPABASE_SERVICE_ROLE_KEY is required",
  );
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
    throw new PayloadValidationError(`${name} must be an object`);
  }
  return value as JsonObject;
}

function optionalObject(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as JsonObject
    : {};
}

function requireString(value: unknown, name: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new PayloadValidationError(`${name} must be a non-empty string`);
  }
  return value;
}

function requirePatternString(
  value: unknown,
  name: string,
  pattern: RegExp,
  description: string,
): string {
  const result = requireString(value, name);
  if (!pattern.test(result)) {
    throw new PayloadValidationError(`${name} must be ${description}`);
  }
  return result;
}

function requireCanonicalUuid(value: unknown, name: string): string {
  return requirePatternString(
    value,
    name,
    UUID_PATTERN,
    "a canonical lowercase UUID",
  );
}

function requireDatabaseUuid(value: unknown, name: string): string {
  const text = requireString(value, name);
  if (!UUID_PATTERN.test(text) && !COMPACT_UUID_PATTERN.test(text)) {
    throw new PayloadValidationError(`${name} must be a UUID`);
  }
  return text;
}

function optionalBoundedString(
  value: unknown,
  name: string,
  maximumLength: number,
): string | null {
  const result = optionalString(value);
  if (result && result.length > maximumLength) {
    throw new PayloadValidationError(
      `record.metadata.${name} must not exceed ${maximumLength} characters`,
    );
  }
  return result;
}

function optionalString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function requireNumber(value: unknown, name: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    throw new PayloadValidationError(`${name} must be a finite number`);
  }
  return value;
}

function requireNonNegativeInteger(value: unknown, name: string): number {
  if (
    typeof value !== "number" || !Number.isSafeInteger(value) || value < 0
  ) {
    throw new PayloadValidationError(`${name} must be a non-negative integer`);
  }
  return value;
}

function optionalNonNegativeInteger(value: unknown): number | null {
  return typeof value === "number" && Number.isSafeInteger(value) && value >= 0
    ? value
    : null;
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
