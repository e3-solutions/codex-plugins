type JsonObject = Record<string, unknown>;

export function sanitizeEventPayload(
  record: JsonObject,
  event: JsonObject,
): JsonObject {
  const eventType = requireString(record.event_type, "record.event_type");
  const turnId = optionalString(record.turn_id);
  const threadId = optionalString(record.thread_id);
  return {
    id: requireString(record.id, "record.id"),
    session_id: requireString(record.session_id, "record.session_id"),
    ...(threadId ? { thread_id: threadId } : {}),
    ...(turnId ? { turn_id: turnId } : {}),
    seq: requireNumber(record.seq, "record.seq"),
    event_type: eventType,
    hook_event_name: optionalString(record.hook_event_name) ??
      optionalString(event.hook_event_name),
    created_at: requireString(record.created_at, "record.created_at"),
    metadata: sanitizeEventMetadata(
      eventType,
      optionalObject(record.metadata),
      optionalObject(event.metadata),
      requireString(record.session_id, "record.session_id"),
    ),
  };
}

function sanitizeEventMetadata(
  eventType: string,
  recordMetadata: JsonObject,
  eventMetadata: JsonObject,
  contextSessionId: string,
): JsonObject {
  const source = { ...eventMetadata, ...recordMetadata };
  const metadata: JsonObject = {};
  copyStringFields(source, metadata, [
    "cwd",
    "transcript_path",
    "model",
    "source",
    "platform",
    "permission_mode",
    // Coding-agent family ("codex" | "claude"). Metadata-only label used to
    // classify the session downstream; carries no user content.
    "agent",
  ]);

  if (eventType === "environment_snapshot") {
    const codexSetup = sanitizeCodexSetup(optionalObject(source.codex_setup));
    if (Object.keys(codexSetup).length > 0) {
      metadata.codex_setup = codexSetup;
    }
    Object.assign(
      metadata,
      sanitizeSeshCueExperiment(recordMetadata, eventMetadata),
    );
    Object.assign(
      metadata,
      sanitizeSeshTelemetry(eventType, source, recordMetadata, eventMetadata),
    );
    return metadata;
  }

  if (SESH_PLAN_CUE_EVENTS.includes(eventType)) {
    Object.assign(
      metadata,
      sanitizeSeshTelemetry(eventType, source, recordMetadata, eventMetadata),
    );
    return metadata;
  }

  if (eventType === "resident_presence") {
    copyStringFields(source, metadata, [
      "thread_source",
      "parent_thread_id",
    ]);
  }

  if (
    [
      "tool_call_started",
      "tool_call_finished",
      "tool_call_failed",
      "tool_permission_requested",
      "tool_permission_denied",
    ].includes(eventType)
  ) {
    copyBooleanFields(source, metadata, ["success"]);
    copyStringFields(source, metadata, [
      "tool_name",
      "tool_phase",
      "tool_call_id",
    ]);
    // Client-attested correlation only; never an authorization or success claim.
    const requestId = source.sesh_request_id;
    if (
      eventType === "tool_call_finished" &&
      isSeshSearchTool(source.tool_name) &&
      source.tool_phase === "finished" &&
      typeof requestId === "string" && requestId.length === 36 &&
      /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/
        .test(requestId)
    ) {
      metadata.sesh_request_id = requestId;
      const linkageKeys = ["tool_name", "tool_phase", "sesh_request_id",
        "sesh_origin_session_id", "sesh_context_session_id", "sesh_origin_basis"];
      const eventHasLinkage = linkageKeys.some((key) => key in eventMetadata);
      const copiesAgree = !eventHasLinkage || linkageKeys.every(
        (key) => recordMetadata[key] === eventMetadata[key],
      );
      const origin = recordMetadata.sesh_origin_session_id;
      const context = recordMetadata.sesh_context_session_id;
      const canonical = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
      if (copiesAgree && recordMetadata.tool_name === source.tool_name &&
          recordMetadata.tool_phase === "finished" && recordMetadata.sesh_request_id === requestId &&
          recordMetadata.sesh_origin_basis === "client_transcript_header_v1" &&
          typeof origin === "string" && origin.length === 36 && canonical.test(origin) &&
          typeof context === "string" && context.length === 36 && canonical.test(context) &&
          context === contextSessionId) {
        metadata.sesh_origin_session_id = origin;
        metadata.sesh_context_session_id = context;
        metadata.sesh_origin_basis = "client_transcript_header_v1";
      }
      // Every request id of a multi-question call, primary first. Both copies must agree.
      const idsKey = "sesh_request_ids";
      const ids = canonicalRequestIdArray(recordMetadata[idsKey], 32);
      const eventHasIds = idsKey in eventMetadata;
      const idsCopiesAgree = !eventHasIds ||
        sameStringArray(ids, canonicalRequestIdArray(eventMetadata[idsKey], 32));
      if (ids !== null && ids[0] === requestId && idsCopiesAgree &&
          recordMetadata.tool_name === source.tool_name &&
          recordMetadata.tool_phase === "finished" &&
          recordMetadata.sesh_request_id === requestId) {
        metadata[idsKey] = ids;
      }
      const deliveredKey = "sesh_delivered_source_handle_sha256_v1";
      const delivered = canonicalDigestArray(recordMetadata[deliveredKey], 75);
      const eventHasDelivered = deliveredKey in eventMetadata;
      const deliveredCopiesAgree = !eventHasDelivered ||
        sameStringArray(delivered, canonicalDigestArray(eventMetadata[deliveredKey], 75));
      if (delivered !== null && deliveredCopiesAgree &&
          recordMetadata.tool_name === source.tool_name &&
          recordMetadata.tool_phase === "finished" &&
          recordMetadata.sesh_request_id === requestId) {
        metadata[deliveredKey] = delivered;
      }
    }

    Object.assign(
      metadata,
      sanitizeSeshTelemetry(eventType, source, recordMetadata, eventMetadata),
    );

    if (
      eventType === "tool_call_finished" &&
      isSeshSourceOpenTool(source.tool_name) &&
      source.tool_phase === "finished"
    ) {
      const digestKey = "sesh_opened_source_handle_sha256_v1";
      const witnessKey = "sesh_source_open_witness_v1";
      const digest = canonicalDigest(recordMetadata[digestKey]);
      const witness = recordMetadata[witnessKey];
      const eventHasSourceOpen = digestKey in eventMetadata || witnessKey in eventMetadata;
      const copiesAgree = !eventHasSourceOpen ||
        (digest === canonicalDigest(eventMetadata[digestKey]) &&
          witness === eventMetadata[witnessKey]);
      const verifiedAllowed = witness !== "verified" ||
        isSeshExactBytesSourceOpenTool(source.tool_name);
      if (digest !== null &&
          (witness === "verified" || witness === "failed" || witness === "unknown") &&
          verifiedAllowed && copiesAgree && recordMetadata.tool_name === source.tool_name &&
          recordMetadata.tool_phase === "finished") {
        metadata[digestKey] = digest;
        metadata[witnessKey] = witness;
      }
    }
  }

  if (
    eventType.startsWith("thread_") ||
    eventType === "tool_batch_finished"
  ) {
    copyBooleanFields(source, metadata, ["stop_hook_active"]);
    copyNumberFields(source, metadata, [
      "prompt_byte_size",
      "tool_batch_size",
    ]);
    copyStringFields(source, metadata, [
      "thread_event",
      "prompt_sha256",
      "stop_reason",
      "error_type",
      "compaction_trigger",
      "session_end_reason",
    ]);
  }

  return metadata;
}

// COR-4681 startup-cue experiment arm. All three keys must be valid and both copies
// must agree, or none is kept. The values are labels only, never content.
const SESH_CUE_EXPERIMENT_KEYS = [
  "sesh_cue_experiment",
  "sesh_cue_arm",
  "sesh_cue_salt",
] as const;
const SESH_CUE_ARMS = ["cue", "no_cue", "optout", "bot", "disabled", "unassigned"];
const SESH_CUE_SALT = /^[0-9]{4}-W(?:0[1-9]|[1-4][0-9]|5[0-3])$/;

function sanitizeSeshCueExperiment(
  recordMetadata: JsonObject,
  eventMetadata: JsonObject,
): JsonObject {
  const experiment = recordMetadata.sesh_cue_experiment;
  const arm = recordMetadata.sesh_cue_arm;
  const salt = recordMetadata.sesh_cue_salt;
  const eventHasExperiment = SESH_CUE_EXPERIMENT_KEYS.some((key) => key in eventMetadata);
  const copiesAgree = !eventHasExperiment || SESH_CUE_EXPERIMENT_KEYS.every(
    (key) => recordMetadata[key] === eventMetadata[key],
  );
  if (
    copiesAgree &&
    experiment === "sesh_cue_v1" &&
    typeof arm === "string" && SESH_CUE_ARMS.includes(arm) &&
    typeof salt === "string" && SESH_CUE_SALT.test(salt)
  ) {
    return { sesh_cue_experiment: experiment, sesh_cue_arm: arm, sesh_cue_salt: salt };
  }
  return {};
}

// COR-4888: Sesh plugin labels (arm, plugin version, timing, guard denials, plan cue fires).
// Off unless CODEX_SESSION_INGEST_SESH_TELEMETRY is 1/true/on, so the default deploy keeps
// exactly today's rows. Labels and numbers only, never content; both copies must agree.
export const SESH_TELEMETRY_ENV = "CODEX_SESSION_INGEST_SESH_TELEMETRY";
const SESH_PLAN_CUE_EVENTS = [
  "sesh_plan_cue_shown", "sesh_plan_cue_withheld",
  // COR-4887 exposure events for the other cues (Codex), same shape.
  "sesh_startup_cue_shown", "sesh_startup_cue_withheld",
  "sesh_past_work_cue_shown", "sesh_past_work_cue_withheld",
];
const SESH_ARMS = ["holdout", "treated", "bot", "optout", "unassigned"];
const SESH_ROUTINE_ARMS = ["routine", "need_only"];
const SESH_PLAN_KINDS = ["service", "migration or table", "script", "module"];
const SESH_PLUGIN_VERSION = /^[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$/;
// Any MCP server name (claude.ai connectors are mcp__<uuid>__sesh__...).
const SESH_TOOL = /^mcp__[A-Za-z0-9_.-]+?__sesh__[A-Za-z0-9_]+$/;

// Labels of one event, not of the session: never merged onto codex_sessions.metadata.
const SESH_PER_EVENT_KEYS = [
  "duration_ms",
  "sesh_subagent",
  "sesh_turn_guard",
  "sesh_plan_cue",
  "sesh_plan_kind",
  "sesh_plan_topic_sha256",
  "sesh_plan_cue_index",
  "sesh_turn_guard_first",
  "sesh_scope",
  "sesh_startup_cue",
  "sesh_past_work_cue",
  "sesh_withheld_by",
];

export function sessionLevelMetadata(metadata: JsonObject): JsonObject {
  const result: JsonObject = { ...metadata };
  for (const key of SESH_PER_EVENT_KEYS) {
    delete result[key];
  }
  return result;
}

function seshTelemetryEnabled(): boolean {
  try {
    const value = (globalThis as { Deno?: { env: { get(name: string): string | undefined } } })
      .Deno?.env.get(SESH_TELEMETRY_ENV);
    return ["1", "true", "on"].includes((value ?? "").trim().toLowerCase());
  } catch {
    return false;
  }
}

function sanitizeSeshTelemetry(
  eventType: string,
  source: JsonObject,
  recordMetadata: JsonObject,
  eventMetadata: JsonObject,
): JsonObject {
  const toolEvent = eventType.startsWith("tool_call_");
  if (
    !seshTelemetryEnabled() ||
    !(toolEvent || eventType === "environment_snapshot" || SESH_PLAN_CUE_EVENTS.includes(eventType))
  ) {
    return {};
  }
  if (
    toolEvent &&
    !(isSeshSearchTool(source.tool_name) ||
      (typeof source.tool_name === "string" && source.tool_name.length <= 256 &&
        SESH_TOOL.test(source.tool_name)))
  ) {
    return {};
  }
  const result: JsonObject = {};
  const agreed = (key: string): unknown => {
    const value = recordMetadata[key];
    if (key in eventMetadata && eventMetadata[key] !== value) {
      return undefined;
    }
    return value;
  };
  const keep = (key: string, valid: (value: unknown) => boolean) => {
    const value = agreed(key);
    if (value !== undefined && valid(value)) {
      result[key] = value;
    }
  };
  const oneOf = (values: string[]) => (value: unknown) =>
    typeof value === "string" && values.includes(value);
  const integer = (minimum: number, maximum: number) => (value: unknown) =>
    typeof value === "number" && Number.isInteger(value) && value >= minimum && value <= maximum;

  keep("sesh_arm", oneOf(SESH_ARMS));
  keep("sesh_routine_arm", oneOf(SESH_ROUTINE_ARMS));
  keep("sesh_holdout_pct", integer(0, 100));
  keep("sesh_plugin_version", (value) =>
    typeof value === "string" && SESH_PLUGIN_VERSION.test(value));
  if (toolEvent) {
    keep("duration_ms", integer(0, 86_400_000));
    keep("sesh_subagent", (value) => value === true);
    if (eventType === "tool_call_started") {
      keep("sesh_turn_guard", oneOf(["denied"]));
      // COR-4887: how this turn's first search ended, and the scope enum the agent asked for.
      if (result.sesh_turn_guard === "denied") {
        keep("sesh_turn_guard_first", oneOf(["succeeded", "failed", "unknown"]));
      }
      if (isSeshSearchTool(source.tool_name)) {
        keep("sesh_scope", oneOf(["all", "mine", "team"]));
      }
    }
  }
  if (SESH_PLAN_CUE_EVENTS.includes(eventType)) {
    keep("sesh_plan_cue", oneOf(["shown", "withheld"]));
    keep("sesh_plan_kind", oneOf(SESH_PLAN_KINDS));
    keep("sesh_plan_topic_sha256", (value) => canonicalDigest(value) !== null);
    keep("sesh_plan_cue_index", integer(1, 2));
    keep("sesh_startup_cue", oneOf(["shown", "withheld"]));
    keep("sesh_past_work_cue", oneOf(["shown", "withheld"]));
    keep("sesh_withheld_by", oneOf(["holdout", "need_only"]));
  }
  return result;
}

// Any namespace serving Sesh search (mcp__codex_apps__cosmos__sesh__..., mcp__e3_mcp__sesh__...),
// mcp__<ns>__search_coding_sessions, or the bare name. Same rule as the client hook.
const SESH_SEARCH_TOOL = /^(?:[A-Za-z0-9_-]+__)?(?:sesh__)?search_coding_sessions$/;

function isSeshSearchTool(value: unknown): boolean {
  return typeof value === "string" && value.length <= 256 && SESH_SEARCH_TOOL.test(value);
}

const CANONICAL_REQUEST_ID =
  /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

function canonicalRequestIdArray(value: unknown, maximum: number): string[] | null {
  if (!Array.isArray(value) || value.length === 0 || value.length > maximum) {
    return null;
  }
  const result: string[] = [];
  const seen = new Set<string>();
  for (const item of value) {
    if (typeof item !== "string" || item.length !== 36 ||
        !CANONICAL_REQUEST_ID.test(item) || seen.has(item)) {
      return null;
    }
    seen.add(item);
    result.push(item);
  }
  return result;
}

function isSeshSourceOpenTool(value: unknown): boolean {
  return ["e3_cosmos", "e3", "cosmos", "cosmos_e3"].some((namespace) =>
    value === `mcp__${namespace}__sesh__open_coding_session_source` ||
    value === `mcp__${namespace}__timetracker__get_chat`
  );
}

function isSeshExactBytesSourceOpenTool(value: unknown): boolean {
  return ["e3_cosmos", "e3", "cosmos", "cosmos_e3"].some((namespace) =>
    value === `mcp__${namespace}__sesh__open_coding_session_source`
  );
}

function canonicalDigest(value: unknown): string | null {
  return typeof value === "string" && /^[0-9a-f]{64}$/.test(value)
    ? value
    : null;
}

function canonicalDigestArray(value: unknown, maximum: number): string[] | null {
  if (!Array.isArray(value) || value.length > maximum) {
    return null;
  }
  const result: string[] = [];
  const seen = new Set<string>();
  for (const item of value) {
    const digest = canonicalDigest(item);
    if (digest === null || seen.has(digest)) {
      return null;
    }
    seen.add(digest);
    result.push(digest);
  }
  return result;
}

function sameStringArray(left: string[] | null, right: string[] | null): boolean {
  return left !== null && right !== null && left.length === right.length &&
    left.every((value, index) => value === right[index]);
}

function sanitizeCodexSetup(value: JsonObject): JsonObject {
  const setup: JsonObject = {};
  const settings = sanitizeObject(optionalObject(value.settings), [
    "model",
    "model_reasoning_effort",
    "plan_mode_reasoning_effort",
    "service_tier",
    "sandbox_mode",
    "personality",
  ]);
  if (Object.keys(settings).length > 0) {
    setup.settings = settings;
  }

  const plugins = sanitizeObjectArray(value.plugins, ["name"], ["enabled"]);
  if (plugins.length > 0) {
    setup.plugins = plugins;
  }

  const skills = sanitizeObjectArray(value.skills, [
    "name",
    "source",
    "marketplace",
    "plugin",
    "version",
  ]);
  if (skills.length > 0) {
    setup.skills = skills;
  }

  const mcpServers = sanitizeObjectArray(value.mcp_servers, [
    "name",
    "transport",
  ]);
  if (mcpServers.length > 0) {
    setup.mcp_servers = mcpServers;
  }

  const marketplaces = sanitizeObjectArray(value.marketplaces, [
    "name",
    "source_type",
  ]);
  if (marketplaces.length > 0) {
    setup.marketplaces = marketplaces;
  }

  const apps = sanitizeObjectArray(value.apps, ["id"]);
  if (apps.length > 0) {
    setup.apps = apps;
  }

  const connections = sanitizeConnections(value.connections);
  if (connections.length > 0) {
    setup.connections = connections;
  }
  return setup;
}

function sanitizeConnections(value: unknown): JsonObject[] {
  if (!Array.isArray(value)) {
    return [];
  }
  const result: JsonObject[] = [];
  for (const item of value) {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      continue;
    }
    const source = item as JsonObject;
    const connection = sanitizeObject(source, ["id"]);
    const tools = Array.isArray(source.tools)
      ? source.tools.filter((tool) =>
        typeof tool === "string" && tool.length > 0
      )
      : [];
    if (tools.length > 0) {
      connection.tools = tools;
    }
    if (Object.keys(connection).length > 0) {
      result.push(connection);
    }
  }
  return result;
}

function sanitizeObjectArray(
  value: unknown,
  stringFields: string[],
  booleanFields: string[] = [],
): JsonObject[] {
  if (!Array.isArray(value)) {
    return [];
  }
  const result: JsonObject[] = [];
  for (const item of value) {
    if (!item || typeof item !== "object" || Array.isArray(item)) {
      continue;
    }
    const sanitized = sanitizeObject(
      item as JsonObject,
      stringFields,
      booleanFields,
    );
    if (Object.keys(sanitized).length > 0) {
      result.push(sanitized);
    }
  }
  return result;
}

function sanitizeObject(
  value: JsonObject,
  stringFields: string[],
  booleanFields: string[] = [],
): JsonObject {
  const result: JsonObject = {};
  copyStringFields(value, result, stringFields);
  copyBooleanFields(value, result, booleanFields);
  return result;
}

function copyStringFields(
  source: JsonObject,
  target: JsonObject,
  fields: string[],
): void {
  for (const field of fields) {
    const value = source[field];
    if (typeof value === "string" && value.length > 0) {
      target[field] = value;
    }
  }
}

function copyBooleanFields(
  source: JsonObject,
  target: JsonObject,
  fields: string[],
): void {
  for (const field of fields) {
    const value = source[field];
    if (typeof value === "boolean") {
      target[field] = value;
    }
  }
}

function copyNumberFields(
  source: JsonObject,
  target: JsonObject,
  fields: string[],
): void {
  for (const field of fields) {
    const value = source[field];
    if (typeof value === "number" && Number.isFinite(value)) {
      target[field] = value;
    }
  }
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

function optionalObject(value: unknown): JsonObject {
  return value && typeof value === "object" && !Array.isArray(value)
    ? value as JsonObject
    : {};
}
