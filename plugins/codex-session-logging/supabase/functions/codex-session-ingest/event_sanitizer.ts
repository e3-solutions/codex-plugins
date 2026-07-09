type JsonObject = Record<string, unknown>;

export function sanitizeEventPayload(
  record: JsonObject,
  event: JsonObject,
): JsonObject {
  const eventType = requireString(record.event_type, "record.event_type");
  const turnId = optionalString(record.turn_id);
  return {
    id: requireString(record.id, "record.id"),
    session_id: requireString(record.session_id, "record.session_id"),
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
    ),
  };
}

function sanitizeEventMetadata(
  eventType: string,
  recordMetadata: JsonObject,
  eventMetadata: JsonObject,
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
  ]);

  if (eventType === "environment_snapshot") {
    const codexSetup = sanitizeCodexSetup(optionalObject(source.codex_setup));
    if (Object.keys(codexSetup).length > 0) {
      metadata.codex_setup = codexSetup;
    }
    return metadata;
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
