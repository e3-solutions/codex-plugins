import "jsr:@supabase/functions-js@2.112.3/edge-runtime.d.ts";

type JsonObject = Record<string, unknown>;

export type IngestDependencies = {
  env(name: string): string | undefined;
  fetch(input: RequestInfo | URL, init?: RequestInit): Promise<Response>;
};

class PayloadError extends Error {}

const BUCKET = "agent-rollouts";
const MAX_BODY_CHARS = 4 * 1024 * 1024;
const MAX_RAW_BYTES = 2 * 1024 * 1024;
const MAX_RECORDS = 1000;
const UUID_PATTERN =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const SAFE_RUN_ID_PATTERN = /^[A-Za-z0-9._:-]{1,200}$/;
const SHA256_PATTERN = /^[0-9a-f]{64}$/;

const defaultDependencies: IngestDependencies = {
  env: (name) => Deno.env.get(name),
  fetch: (input, init) => fetch(input, init),
};

export async function handleRequest(
  request: Request,
  dependencies: IngestDependencies = defaultDependencies,
): Promise<Response> {
  if (request.method === "OPTIONS") {
    return new Response(null, { status: 204, headers: corsHeaders() });
  }
  if (request.method !== "POST") {
    return jsonResponse({ error: "method_not_allowed" }, 405);
  }

  try {
    const configuredToken = requiredEnv(
      dependencies,
      "CODEX_AGENT_INGEST_TOKEN",
    );
    const providedToken = request.headers.get("x-codex-agent-ingest-token") ??
      "";
    if (!await constantTimeEqual(configuredToken, providedToken)) {
      return jsonResponse({ error: "unauthorized" }, 401);
    }

    const workspaceId = requiredEnv(dependencies, "CODEX_AGENT_WORKSPACE_ID");
    if (!UUID_PATTERN.test(workspaceId)) {
      throw new Error("CODEX_AGENT_WORKSPACE_ID must be a UUID");
    }

    const envelope = await parseEnvelope(request);
    const run = requireObject(envelope.run, "run");
    const runId = requireString(run.id, "run.id");
    if (!SAFE_RUN_ID_PATTERN.test(runId)) {
      throw new PayloadError("run.id is invalid");
    }

    const installation = requireObject(envelope.installation, "installation");
    requireUuid(installation.id, "installation.id");
    requireString(installation.actor_key, "installation.actor_key");

    const records = requireRecords(envelope.records);
    const raw = requireObject(envelope.raw, "raw");
    const rawBytes = decodeBase64(
      requireString(raw.data_base64, "raw.data_base64"),
    );
    if (rawBytes.byteLength < 1 || rawBytes.byteLength > MAX_RAW_BYTES) {
      throw new PayloadError(
        `raw bytes must be between 1 and ${MAX_RAW_BYTES}`,
      );
    }

    const suppliedSha256 = requirePattern(
      raw.sha256,
      "raw.sha256",
      SHA256_PATTERN,
    );
    const actualSha256 = await sha256Hex(rawBytes);
    if (actualSha256 !== suppliedSha256) {
      throw new PayloadError("raw.sha256 does not match raw.data_base64");
    }

    validateRecordBounds(records, rawBytes.byteLength);

    const encoding = optionalString(raw.encoding) ?? "identity";
    if (encoding !== "identity" && encoding !== "gzip") {
      throw new PayloadError("raw.encoding must be identity or gzip");
    }

    const extension = encoding === "gzip" ? ".ndjson.gz" : ".ndjson";
    const rawPath =
      `workspaces/${workspaceId}/runs/${runId}/batches/${actualSha256}${extension}`;
    const supabaseUrl = requiredEnv(dependencies, "SUPABASE_URL").replace(
      /\/$/,
      "",
    );
    const serviceRoleKey = requiredEnv(
      dependencies,
      "SUPABASE_SERVICE_ROLE_KEY",
    );

    await uploadRawObject(
      dependencies,
      supabaseUrl,
      serviceRoleKey,
      rawPath,
      rawBytes,
      encoding,
    );

    const commitResult = await commitProjection(
      dependencies,
      supabaseUrl,
      serviceRoleKey,
      workspaceId,
      installation,
      run,
      records,
      {
        bucket: BUCKET,
        path: rawPath,
        sha256: actualSha256,
        byte_length: rawBytes.byteLength,
      },
    );

    return jsonResponse(commitResult, 200);
  } catch (error) {
    if (error instanceof PayloadError) {
      return jsonResponse(
        { error: "invalid_payload", message: error.message },
        400,
      );
    }
    const message = error instanceof Error ? error.message : String(error);
    return jsonResponse({ error: "ingest_failed", message }, 500);
  }
}

async function parseEnvelope(request: Request): Promise<JsonObject> {
  const text = await request.text();
  if (text.length > MAX_BODY_CHARS) {
    throw new PayloadError("request body is too large");
  }
  let value: unknown;
  try {
    value = JSON.parse(text);
  } catch {
    throw new PayloadError("request body must be valid JSON");
  }
  const envelope = requireObject(value, "body");
  if (envelope.schema_version !== 1) {
    throw new PayloadError("schema_version must be 1");
  }
  return envelope;
}

function requireRecords(value: unknown): JsonObject[] {
  if (!Array.isArray(value) || value.length < 1 || value.length > MAX_RECORDS) {
    throw new PayloadError(
      `records must contain between 1 and ${MAX_RECORDS} items`,
    );
  }
  return value.map((item, index) => {
    const record = requireObject(item, `records[${index}]`);
    requireString(record.source, `records[${index}].source`);
    requireString(record.record_key, `records[${index}].record_key`);
    requireString(record.record_kind, `records[${index}].record_kind`);
    requireTimestamp(record.occurred_at, `records[${index}].occurred_at`);
    requireObject(record.payload, `records[${index}].payload`);
    return record;
  });
}

function validateRecordBounds(
  records: JsonObject[],
  rawByteLength: number,
): void {
  const seen = new Set<string>();
  for (const record of records) {
    const identity = `${record.source}\u0000${record.record_key}`;
    if (seen.has(identity)) {
      throw new PayloadError("records contain a duplicate source/record_key");
    }
    seen.add(identity);

    const offset = optionalNonNegativeInteger(
      record.raw_byte_offset,
      "raw_byte_offset",
    );
    const length = optionalNonNegativeInteger(
      record.raw_byte_length,
      "raw_byte_length",
    );
    if (offset !== null && length !== null && offset + length > rawByteLength) {
      throw new PayloadError(
        "record raw byte range exceeds the uploaded object",
      );
    }
  }
}

async function uploadRawObject(
  dependencies: IngestDependencies,
  supabaseUrl: string,
  serviceRoleKey: string,
  path: string,
  bytes: Uint8Array,
  encoding: string,
): Promise<void> {
  const encodedPath = path.split("/").map(encodeURIComponent).join("/");
  const body = bytes.buffer.slice(
    bytes.byteOffset,
    bytes.byteOffset + bytes.byteLength,
  ) as ArrayBuffer;
  const response = await dependencies.fetch(
    `${supabaseUrl}/storage/v1/object/${
      encodeURIComponent(BUCKET)
    }/${encodedPath}`,
    {
      method: "POST",
      headers: {
        apikey: serviceRoleKey,
        authorization: `Bearer ${serviceRoleKey}`,
        "content-type": encoding === "gzip"
          ? "application/gzip"
          : "application/x-ndjson",
        "cache-control": "no-store",
        "x-upsert": "false",
      },
      body,
    },
  );
  if (response.ok) return;

  const message = await response.text();
  const duplicate = (response.status === 400 || response.status === 409) &&
    /duplicate|already exists/i.test(message);
  if (!duplicate) {
    throw new Error(`raw upload failed (${response.status}): ${message}`);
  }
}

async function commitProjection(
  dependencies: IngestDependencies,
  supabaseUrl: string,
  serviceRoleKey: string,
  workspaceId: string,
  installation: JsonObject,
  run: JsonObject,
  records: JsonObject[],
  raw: JsonObject,
): Promise<JsonObject> {
  const response = await dependencies.fetch(
    `${supabaseUrl}/rest/v1/rpc/commit_agent_batch_v1`,
    {
      method: "POST",
      headers: {
        apikey: serviceRoleKey,
        authorization: `Bearer ${serviceRoleKey}`,
        "content-type": "application/json",
      },
      body: JSON.stringify({
        p_workspace_id: workspaceId,
        p_installation: installation,
        p_run: run,
        p_records: records,
        p_raw: raw,
      }),
    },
  );
  const text = await response.text();
  if (!response.ok) {
    throw new Error(`projection commit failed (${response.status}): ${text}`);
  }
  const value: unknown = JSON.parse(text);
  return requireObject(value, "commit result");
}

function decodeBase64(value: string): Uint8Array {
  let binary: string;
  try {
    binary = atob(value);
  } catch {
    throw new PayloadError("raw.data_base64 is invalid base64");
  }
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes;
}

async function sha256Hex(value: Uint8Array | string): Promise<string> {
  const bytes = typeof value === "string"
    ? new TextEncoder().encode(value)
    : value;
  const input = bytes.buffer.slice(
    bytes.byteOffset,
    bytes.byteOffset + bytes.byteLength,
  ) as ArrayBuffer;
  const digest = new Uint8Array(await crypto.subtle.digest("SHA-256", input));
  return [...digest].map((byte) => byte.toString(16).padStart(2, "0")).join("");
}

async function constantTimeEqual(
  left: string,
  right: string,
): Promise<boolean> {
  const [leftHash, rightHash] = await Promise.all([
    sha256Hex(left),
    sha256Hex(right),
  ]);
  let difference = left.length === right.length ? 0 : 1;
  for (let index = 0; index < leftHash.length; index += 1) {
    difference |= leftHash.charCodeAt(index) ^ rightHash.charCodeAt(index);
  }
  return difference === 0;
}

function requireObject(value: unknown, label: string): JsonObject {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    throw new PayloadError(`${label} must be an object`);
  }
  return value as JsonObject;
}

function requireString(value: unknown, label: string): string {
  if (typeof value !== "string" || value.length === 0) {
    throw new PayloadError(`${label} must be a non-empty string`);
  }
  return value;
}

function requireUuid(value: unknown, label: string): string {
  return requirePattern(value, label, UUID_PATTERN);
}

function requirePattern(
  value: unknown,
  label: string,
  pattern: RegExp,
): string {
  const result = requireString(value, label);
  if (!pattern.test(result)) throw new PayloadError(`${label} is invalid`);
  return result;
}

function requireTimestamp(value: unknown, label: string): string {
  const result = requireString(value, label);
  if (Number.isNaN(Date.parse(result))) {
    throw new PayloadError(`${label} must be an ISO-8601 timestamp`);
  }
  return result;
}

function optionalString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function optionalNonNegativeInteger(
  value: unknown,
  label: string,
): number | null {
  if (value === undefined || value === null) return null;
  if (!Number.isSafeInteger(value) || Number(value) < 0) {
    throw new PayloadError(`${label} must be a non-negative safe integer`);
  }
  return Number(value);
}

function requiredEnv(dependencies: IngestDependencies, name: string): string {
  const value = dependencies.env(name);
  if (!value) throw new Error(`${name} is required`);
  return value;
}

function corsHeaders(): HeadersInit {
  return {
    "access-control-allow-origin": "*",
    "access-control-allow-headers":
      "content-type,x-codex-agent-ingest-token,x-client-info",
    "access-control-allow-methods": "POST,OPTIONS",
  };
}

function jsonResponse(body: unknown, status: number): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json", ...corsHeaders() },
  });
}

if (import.meta.main) {
  Deno.serve((request) => handleRequest(request));
}
