const STORAGE_KEY = "careerdesk_trusted_immediate_operation_outbox_v3";
const CONTRACT_VERSION = 3;
export const MAX_TRUSTED_IMMEDIATE_OPERATION_RECOVERY_TURNS = 128;
const MAX_AGE_MS = 30 * 24 * 60 * 60 * 1_000;
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const SCOPE_PATTERN = /^[0-9a-f]{64}$/;
const ACTION_COMMAND_KEYS = new Set([
  "operation_type",
  "operation_id",
  "command_id",
  "client_turn_id",
]);

export type TrustedImmediateOperationType =
  | "application_update"
  | "review_timeline_entry_edit"
  | "review_record"
  | "preference_update";

// Only operation families with direct undo commands belong in the action outbox.
// This is an explicit allow-list: adding a future receipt family must not silently
// grant it an invented undo command or browser persistence.
export type ActionOperationType = Extract<
  TrustedImmediateOperationType,
  "application_update" | "review_timeline_entry_edit"
>;

export function trustedImmediateOperationTypeFromServer(
  value: unknown,
): TrustedImmediateOperationType | null {
  switch (value) {
    case "application_update":
    case "review_timeline_entry_edit":
    case "review_record":
    case "preference_update":
      return value;
    default:
      return null;
  }
}

export type TrustedImmediateOperationActionCommand = {
  operationType: ActionOperationType;
  operationId: string;
  commandId: string;
  clientTurnId: string;
};

export type TrustedImmediateOperationOutbox = {
  turnIds: string[];
  uncertainTurnIds: string[];
  uncertainActionCommands: TrustedImmediateOperationActionCommand[];
  dispatchedTurnIds: string[];
};

type ParsedEnvelope = {
  outbox: TrustedImmediateOperationOutbox;
  createdAt: number;
  lastSemanticChangeAt: number;
};

function emptyOutbox(): TrustedImmediateOperationOutbox {
  return {
    turnIds: [],
    uncertainTurnIds: [],
    uncertainActionCommands: [],
    dispatchedTurnIds: [],
  };
}

function storage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage;
  } catch {
    return null;
  }
}

function ids(value: unknown, truncate = false): string[] | null {
  if (!Array.isArray(value)
      || (!truncate && value.length > MAX_TRUSTED_IMMEDIATE_OPERATION_RECOVERY_TURNS)) return null;
  const source = truncate
    ? value.slice(-MAX_TRUSTED_IMMEDIATE_OPERATION_RECOVERY_TURNS)
    : value;
  const unique: string[] = [];
  const seen = new Set<string>();
  for (const item of source) {
    if (typeof item !== "string" || !UUID_PATTERN.test(item)) return null;
    if (seen.has(item)) continue;
    seen.add(item);
    unique.push(item);
  }
  return unique;
}

function actionCommands(
  value: unknown,
  { truncate = false } = {},
): TrustedImmediateOperationActionCommand[] | null {
  if (!Array.isArray(value)
      || (!truncate && value.length > MAX_TRUSTED_IMMEDIATE_OPERATION_RECOVERY_TURNS)) return null;
  const source = truncate
    ? value.slice(-MAX_TRUSTED_IMMEDIATE_OPERATION_RECOVERY_TURNS)
    : value;
  const commands: TrustedImmediateOperationActionCommand[] = [];
  const seenOperations = new Set<string>();
  const seenCommands = new Set<string>();
  for (const item of source) {
    if (item === null || typeof item !== "object" || Array.isArray(item)) return null;
    const record = item as Record<string, unknown>;
    const operationType = record.operation_type;
    if (Object.keys(record).some((key) => !ACTION_COMMAND_KEYS.has(key))
        || (operationType !== "application_update"
          && operationType !== "review_timeline_entry_edit")
        || typeof record.operation_id !== "string"
        || typeof record.command_id !== "string"
        || typeof record.client_turn_id !== "string"
        || !UUID_PATTERN.test(record.operation_id)
        || !UUID_PATTERN.test(record.command_id)
        || !UUID_PATTERN.test(record.client_turn_id)
        || seenOperations.has(record.operation_id)
        || seenCommands.has(record.command_id)) return null;
    seenOperations.add(record.operation_id);
    seenCommands.add(record.command_id);
    commands.push({
      operationType,
      operationId: record.operation_id,
      commandId: record.command_id,
      clientTurnId: record.client_turn_id,
    });
  }
  return commands;
}

function remove(storageArea: Storage, key: string): boolean {
  try {
    storageArea.removeItem(key);
    return storageArea.getItem(key) === null;
  } catch {
    // Privacy mode or browser policy may block sessionStorage; memory state remains correct.
    return false;
  }
}

function parseEnvelope(
  raw: string,
  recoveryScope: string,
): ParsedEnvelope | null {
  try {
    const parsed = JSON.parse(raw) as unknown;
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) return null;
    const record = parsed as Record<string, unknown>;
    const allowedKeys = new Set([
      "version",
      "recovery_scope",
      "created_at",
      "last_semantic_change_at",
      "turn_ids",
      "uncertain_turn_ids",
      "uncertain_action_commands",
      "dispatched_turn_ids",
    ]);
    if (Object.keys(record).some((key) => !allowedKeys.has(key))) return null;
    const turnIds = ids(record.turn_ids);
    const uncertainTurnIds = ids(record.uncertain_turn_ids);
    const uncertainActionCommands = actionCommands(record.uncertain_action_commands);
    const dispatchedTurnIds = ids(record.dispatched_turn_ids);
    const now = Date.now();
    if (record.version !== CONTRACT_VERSION
        || record.recovery_scope !== recoveryScope
        || !SCOPE_PATTERN.test(recoveryScope)
        || typeof record.created_at !== "number"
        || !Number.isFinite(record.created_at)
        || typeof record.last_semantic_change_at !== "number"
        || !Number.isFinite(record.last_semantic_change_at)
        || record.created_at > record.last_semantic_change_at
        || now - record.last_semantic_change_at > MAX_AGE_MS
        || record.last_semantic_change_at > now + 60_000
        || turnIds === null
        || uncertainTurnIds === null
        || uncertainActionCommands === null
        || dispatchedTurnIds === null) return null;
    const turnSet = new Set(turnIds);
    if (uncertainActionCommands.some((item) => !turnSet.has(item.clientTurnId))) return null;
    return {
      outbox: {
        turnIds,
        uncertainTurnIds: uncertainTurnIds.filter((item) => turnSet.has(item)),
        uncertainActionCommands,
        dispatchedTurnIds,
      },
      createdAt: record.created_at,
      lastSemanticChangeAt: record.last_semantic_change_at,
    };
  } catch {
    return null;
  }
}

function serialize(outbox: TrustedImmediateOperationOutbox): object {
  return {
    turn_ids: outbox.turnIds,
    uncertain_turn_ids: outbox.uncertainTurnIds,
    uncertain_action_commands: outbox.uncertainActionCommands.map((item) => ({
      operation_type: item.operationType,
      operation_id: item.operationId,
      command_id: item.commandId,
      client_turn_id: item.clientTurnId,
    })),
    dispatched_turn_ids: outbox.dispatchedTurnIds,
  };
}

function persistEnvelope(
  storageArea: Storage,
  recoveryScope: string,
  outbox: TrustedImmediateOperationOutbox,
  createdAt: number,
  lastSemanticChangeAt: number,
): boolean {
  try {
    const raw = JSON.stringify({
      version: CONTRACT_VERSION,
      recovery_scope: recoveryScope,
      created_at: createdAt,
      last_semantic_change_at: lastSemanticChangeAt,
      ...serialize(outbox),
    });
    storageArea.setItem(STORAGE_KEY, raw);
    return storageArea.getItem(STORAGE_KEY) === raw;
  } catch {
    return false;
  }
}

function readEnvelope(recoveryScope: string): ParsedEnvelope | null {
  const storageArea = storage();
  if (storageArea === null || !SCOPE_PATTERN.test(recoveryScope)) return null;
  try {
    const raw = storageArea.getItem(STORAGE_KEY);
    if (raw === null) return null;
    const envelope = parseEnvelope(raw, recoveryScope);
    if (envelope === null) remove(storageArea, STORAGE_KEY);
    return envelope;
  } catch {
    remove(storageArea, STORAGE_KEY);
    return null;
  }
}

export function readTrustedImmediateOperationOutbox(
  recoveryScope: string,
): TrustedImmediateOperationOutbox {
  return readEnvelope(recoveryScope)?.outbox ?? emptyOutbox();
}

/**
 * Three-tier retention: action turns, unresolved non-action turns, then resolved turns.
 * Returns retained turn IDs in source order so outbox persistence and panels share logic.
 */
export function selectRetainedRecoveryTurnIds(
  orderedTurnIds: readonly string[],
  actionTurnIds: ReadonlySet<string>,
  uncertainTurnIds: ReadonlySet<string>,
  max: number,
): string[] {
  const retainedActions = orderedTurnIds
    .filter((item) => actionTurnIds.has(item))
    .slice(-max);
  const afterActions = max - retainedActions.length;
  const retainedUncertain = afterActions === 0
    ? []
    : orderedTurnIds
      .filter((item) => uncertainTurnIds.has(item) && !actionTurnIds.has(item))
      .slice(-afterActions);
  const afterUncertain = afterActions - retainedUncertain.length;
  const retainedResolved = afterUncertain === 0
    ? []
    : orderedTurnIds
      .filter((item) => !uncertainTurnIds.has(item) && !actionTurnIds.has(item))
      .slice(-afterUncertain);
  const retained = new Set([...retainedActions, ...retainedUncertain, ...retainedResolved]);
  return orderedTurnIds.filter((item) => retained.has(item));
}

function normalizedOutbox(
  outbox: TrustedImmediateOperationOutbox,
): TrustedImmediateOperationOutbox | null {
  const turnIds = ids(outbox.turnIds, true);
  const uncertainTurnIds = ids(outbox.uncertainTurnIds, true);
  const uncertainActionCommands = actionCommands(outbox.uncertainActionCommands.map((item) => ({
    operation_type: item.operationType,
    operation_id: item.operationId,
    command_id: item.commandId,
    client_turn_id: item.clientTurnId,
  })), { truncate: true });
  const dispatchedTurnIds = ids(outbox.dispatchedTurnIds, true);
  if (turnIds === null || uncertainTurnIds === null || uncertainActionCommands === null
      || dispatchedTurnIds === null) return null;

  const actionTurnIds = new Set(
    uncertainActionCommands.map((item) => item.clientTurnId),
  );
  const uncertainTurnIdSet = new Set(uncertainTurnIds);
  const allTurnIds = [...new Set([
    ...turnIds,
    ...uncertainActionCommands.map((item) => item.clientTurnId),
  ])];
  const retainedTurnIds = selectRetainedRecoveryTurnIds(
    allTurnIds,
    actionTurnIds,
    uncertainTurnIdSet,
    MAX_TRUSTED_IMMEDIATE_OPERATION_RECOVERY_TURNS,
  );
  const retainedSet = new Set(retainedTurnIds);
  return {
    turnIds: retainedTurnIds,
    uncertainTurnIds: uncertainTurnIds.filter((item) => retainedSet.has(item)),
    uncertainActionCommands: uncertainActionCommands.filter(
      (item) => retainedSet.has(item.clientTurnId),
    ),
    dispatchedTurnIds,
  };
}

function write(recoveryScope: string, outbox: TrustedImmediateOperationOutbox): boolean {
  const storageArea = storage();
  if (storageArea === null || !SCOPE_PATTERN.test(recoveryScope)) return false;
  const normalized = normalizedOutbox(outbox);
  if (normalized === null) return false;
  if (normalized.turnIds.length === 0
      && normalized.uncertainActionCommands.length === 0
      && normalized.dispatchedTurnIds.length === 0) {
    return remove(storageArea, STORAGE_KEY);
  }
  const existing = readEnvelope(recoveryScope);
  const nextSemantic = serialize(normalized);
  if (existing !== null
      && JSON.stringify(serialize(existing.outbox)) === JSON.stringify(nextSemantic)) return true;
  const now = Date.now();
  const persisted = persistEnvelope(
    storageArea,
    recoveryScope,
    normalized,
    existing?.createdAt ?? now,
    now,
  );
  return persisted;
}

export function storeTrustedImmediateOperationTurns(
  recoveryScope: string,
  turnIds: string[],
  uncertainTurnIds: string[],
): boolean {
  const current = readTrustedImmediateOperationOutbox(recoveryScope);
  return write(recoveryScope, { ...current, turnIds, uncertainTurnIds });
}

export function storeUncertainTrustedImmediateOperationActions(
  recoveryScope: string,
  commands: TrustedImmediateOperationActionCommand[],
): boolean {
  const current = readTrustedImmediateOperationOutbox(recoveryScope);
  return write(recoveryScope, { ...current, uncertainActionCommands: commands });
}

export function markTrustedImmediateOperationTurnDispatched(
  recoveryScope: string,
  clientTurnId: string,
): boolean {
  if (!UUID_PATTERN.test(clientTurnId)) return false;
  const current = readTrustedImmediateOperationOutbox(recoveryScope);
  return write(recoveryScope, {
    ...current,
    dispatchedTurnIds: [...current.dispatchedTurnIds.filter(
      (item) => item !== clientTurnId,
    ), clientTurnId],
  });
}

export function settleTrustedImmediateOperationDispatchedTurn(
  recoveryScope: string,
  clientTurnId: string,
  keepCandidate: boolean,
  uncertain: boolean,
): boolean {
  const current = readTrustedImmediateOperationOutbox(recoveryScope);
  const dispatchedTurnIds = current.dispatchedTurnIds.filter((item) => item !== clientTurnId);
  const turnIds = current.turnIds.filter((item) => item !== clientTurnId);
  const uncertainTurnIds = current.uncertainTurnIds.filter((item) => item !== clientTurnId);
  return write(recoveryScope, {
    ...current,
    dispatchedTurnIds,
    turnIds: keepCandidate ? [...turnIds, clientTurnId] : turnIds,
    uncertainTurnIds: keepCandidate && uncertain
      ? [...uncertainTurnIds, clientTurnId]
      : uncertainTurnIds,
  });
}

export function recoverDispatchedTrustedImmediateOperationTurns(
  recoveryScope: string,
): TrustedImmediateOperationOutbox {
  const current = readTrustedImmediateOperationOutbox(recoveryScope);
  if (current.dispatchedTurnIds.length === 0) return current;
  const turnIds = [...new Set([...current.turnIds, ...current.dispatchedTurnIds])];
  const uncertainTurnIds = [
    ...new Set([...current.uncertainTurnIds, ...current.dispatchedTurnIds]),
  ];
  const promoted = normalizedOutbox({
    ...current,
    turnIds,
    uncertainTurnIds,
    dispatchedTurnIds: [],
  });
  if (promoted === null) return current;
  // Reconcile pre-POST markers on mount. If cleanup persistence fails, retain the marker
  // so the next mount promotes it again instead of regressing to dispatched-only.
  write(recoveryScope, promoted);
  return promoted;
}
