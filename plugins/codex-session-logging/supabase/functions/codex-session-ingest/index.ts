import { userIdForClientIdentity } from "./client_identity.ts";
import { sanitizeEventPayload } from "./event_sanitizer.ts";

type JsonObject = Record<string, unknown>;

class PayloadValidationError extends Error {}

const DEFAULT_BUCKET = "codex-sessions";
const DEFAULT_ALLOWED_ORG = "e3-solutions";
const MAX_ROLLOUT_CHUNK_BYTES = 1024 * 1024;
const MAX_ROLLOUT_CHUNK_BASE64_LENGTH = Math.ceil(MAX_ROLLOUT_CHUNK_BYTES / 3) *
  4;
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
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
    const tokenError = optionalIngestTokenError(req);
    if (tokenError) {
      return tokenError;
    }

    const body = await requestJson(req);
    const payload = requireObject(body, "payload");
    const client = requireObject(payload.client, "client");

    const remote = requireString(client.repo_remote, "client.repo_remote");
    if (!remoteBelongsToOrg(remote, allowedGithubOrg())) {
      return jsonResponse({ error: "repo_not_allowed" }, 403);
    }

    const payloadKind = optionalString(payload.kind);
    if (payloadKind !== "rollout_chunk" && isHistoricalBackfill(payload)) {
      return jsonResponse({
        ok: true,
        ignored: true,
        reason: "historical_backfill_disabled",
      });
    }

    const userId = await userIdForClientIdentity(client);
    if (payloadKind === "rollout_chunk") {
      return await ingestRolloutChunk(payload, client, userId, remote);
    }

    if (payloadKind === "backfill_status") {
      const backfill = requireObject(payload.backfill, "backfill");
      const observedAt = requireString(
        backfill.updated_at,
        "backfill.updated_at",
      );
      await upsertSessionUser({ created_at: observedAt }, client, userId);
      await upsertBackfillRun(backfill, client, userId);
      return jsonResponse({ ok: true, kind: "backfill_status" });
    }

    const record = requireObject(payload.record, "record");
    const recordType = optionalString(record.type) ?? "message";
    if (recordType === "usage") {
      const usage = requireObject(payload.usage, "usage");
      await upsertSessionUser(record, client, userId);
      await upsertSession(record, client, userId, remote);
      await upsertSessionUsage(record, userId, usage);
      return jsonResponse({
        ok: true,
        id: record.id,
        kind: "usage",
      });
    }
    const storagePath = storagePathForRecord(record, userId);

    if (recordType === "event") {
      const event = requireObject(payload.event, "event");
      const sanitizedEvent = sanitizeEventPayload(record, event);
      await upsertSessionUser(record, client, userId);
      await uploadStorageObject(storagePath, sanitizedEvent);
      await upsertSession(
        record,
        client,
        userId,
        remote,
        optionalObject(sanitizedEvent.metadata),
      );
      await upsertEvent(record, userId, storagePath, sanitizedEvent);
      return jsonResponse({
        ok: true,
        id: record.id,
        storage_path: storagePath,
      });
    }

    const message = requireObject(payload.message, "message");
    await validateMessageIntegrity(record, message);
    await upsertSessionUser(record, client, userId);
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
    if (error instanceof PayloadValidationError) {
      return jsonResponse({ error: "invalid_payload", message }, 400);
    }
    return jsonResponse({ error: "ingest_failed", message }, 500);
  }
}

async function ingestRolloutChunk(
  payload: JsonObject,
  client: JsonObject,
  userId: string,
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
  const storagePath = rolloutStoragePath(
    userId,
    sessionId,
    fileGeneration,
    startOffset,
    endOffset,
    contentSha256,
  );
  const event = { metadata };

  await upsertSessionUser(catalogRecord, client, userId);
  await uploadStorageBytes(
    storagePath,
    exactArrayBuffer(content),
    "application/x-ndjson",
  );
  await upsertSession(catalogRecord, client, userId, remote, metadata);
  await upsertEvent(catalogRecord, userId, storagePath, event);
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

async function upsertBackfillRun(
  backfill: JsonObject,
  client: JsonObject,
  userId: string,
): Promise<void> {
  const totals = optionalObject(backfill.totals);
  const installationId = requireString(
    client.installation_id,
    "client.installation_id",
  );
  const updatedAt = requireString(backfill.updated_at, "backfill.updated_at");
  const row = {
    user_id: userId,
    installation_id: installationId,
    backfill_version: requireNumber(backfill.version, "backfill.version"),
    status: requireBackfillStatus(backfill.status),
    files_discovered: optionalNonNegativeNumber(totals.discovered),
    files_processed: optionalNonNegativeNumber(totals.processed),
    records_queued: optionalNonNegativeNumber(totals.queued),
    files_skipped_non_e3: optionalNonNegativeNumber(totals.skipped_non_e3),
    files_failed: optionalNonNegativeNumber(totals.failed),
    remaining_files: optionalNonNegativeNumber(backfill.remaining_files),
    started_at: optionalString(backfill.started_at),
    completed_at: optionalString(backfill.completed_at),
    last_heartbeat_at: updatedAt,
    updated_at: updatedAt,
    metadata: {
      plugin_version: optionalString(
        optionalObject(backfill.metadata).plugin_version,
      ),
      last_drain: sanitizeDrainResult(
        optionalObject(optionalObject(backfill.metadata).last_drain),
      ),
    },
  };
  await restUpsert(
    "codex_session_backfill_runs",
    row,
    "user_id,installation_id,backfill_version",
  );
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

function allowedGithubOrg(): string {
  return Deno.env.get("CODEX_SESSION_LOG_ALLOWED_GITHUB_ORG") ??
    DEFAULT_ALLOWED_ORG;
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
  const bucket = Deno.env.get("CODEX_SESSION_LOG_BUCKET") ?? DEFAULT_BUCKET;
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

async function upsertSession(
  record: JsonObject,
  client: JsonObject,
  userId: string,
  remote: string,
  metadata = optionalObject(record.metadata),
): Promise<void> {
  const sessionId = requireString(record.session_id, "record.session_id");
  const existing = await existingSession(sessionId);
  const threadId = existing.threadId ??
    optionalString(record.thread_id) ?? await sha256Hex(sessionId);
  const sessionMetadata = await sessionMetadataForUpsert(
    metadata,
    client,
    existing.metadata,
  );
  const row = {
    id: sessionId,
    thread_id: threadId,
    user_id: userId,
    repo: remote,
    branch: optionalString(client.git_branch),
    storage_prefix: `users/${safeSegment(userId)}/sessions/${
      safeSegment(sessionId)
    }`,
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
): Promise<{
  metadata: JsonObject;
  threadId: string | null;
  startedAt: string | null;
}> {
  const response = await supabaseFetch(
    `/rest/v1/codex_sessions?select=metadata,thread_id,started_at&id=eq.${
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
    return { metadata: {}, threadId: null, startedAt: null };
  }
  const row = optionalObject(rows[0]);
  return {
    metadata: optionalObject(row.metadata),
    threadId: optionalString(row.thread_id),
    startedAt: optionalString(row.started_at),
  };
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
    storage_bucket: Deno.env.get("CODEX_SESSION_LOG_BUCKET") ?? DEFAULT_BUCKET,
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

async function upsertSessionUsage(
  record: JsonObject,
  userId: string,
  usage: JsonObject,
): Promise<void> {
  const modelContextWindow = optionalNonNegativeInteger(
    usage.model_context_window,
  );
  const row: JsonObject = {
    session_id: requireString(record.session_id, "record.session_id"),
    user_id: userId,
    input_tokens: requireNonNegativeInteger(
      usage.input_tokens,
      "usage.input_tokens",
    ),
    cached_input_tokens: requireNonNegativeInteger(
      usage.cached_input_tokens,
      "usage.cached_input_tokens",
    ),
    output_tokens: requireNonNegativeInteger(
      usage.output_tokens,
      "usage.output_tokens",
    ),
    reasoning_output_tokens: requireNonNegativeInteger(
      usage.reasoning_output_tokens,
      "usage.reasoning_output_tokens",
    ),
    total_tokens: requireNonNegativeInteger(
      usage.total_tokens,
      "usage.total_tokens",
    ),
    observed_at: requireString(usage.created_at, "usage.created_at"),
    metadata: optionalObject(usage.metadata),
    updated_at: new Date().toISOString(),
  };
  if (modelContextWindow !== null) {
    row.model_context_window = modelContextWindow;
  }
  await restUpsert("codex_session_usage", row, "session_id");
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
    storage_bucket: Deno.env.get("CODEX_SESSION_LOG_BUCKET") ?? DEFAULT_BUCKET,
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

function optionalNonNegativeNumber(value: unknown): number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0
    ? value
    : 0;
}

function sanitizeDrainResult(value: JsonObject): JsonObject {
  return {
    uploaded: optionalNonNegativeNumber(value.uploaded),
    failed: optionalNonNegativeNumber(value.failed),
    dead_lettered: optionalNonNegativeNumber(value.dead_lettered),
    historical_dead_lettered: optionalNonNegativeNumber(
      value.historical_dead_lettered,
    ),
    remaining: optionalNonNegativeNumber(value.remaining),
  };
}

function requireBackfillStatus(value: unknown): string {
  const status = requireString(value, "backfill.status");
  if (!["running", "partial", "complete", "failed"].includes(status)) {
    throw new PayloadValidationError("backfill.status is invalid");
  }
  return status;
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
