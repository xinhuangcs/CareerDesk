export type ProposalSurface =
  | "intake"
  | "application_merge"
  | "application_delete"
  | "review_undo";

export type ProposalOperation = {
  surface: ProposalSurface;
  operationId: string;
};

export type ProposalRecovery = {
  operations: ProposalOperation[];
  reviewTurnIds: string[];
};

const PROPOSAL_RECOVERY_STORAGE_KEY = "careerdesk.chat.proposalRecovery.v3";
const LEGACY_PROPOSAL_RECOVERY_STORAGE_KEY = "careerdesk.chat.proposalRecovery.v2";
const VISIBLE_TURNS_STORAGE_KEY = "careerdesk.chat.visibleOperationTurns.v1";
const SETTLED_PROPOSALS_STORAGE_KEY = "careerdesk.chat.settledProposalOperations.v1";
const PROPOSAL_SURFACE_VALUES: ProposalSurface[] = [
  "intake",
  "application_merge",
  "application_delete",
  "review_undo",
];
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const SCOPE_PATTERN = /^[0-9a-f]{64}$/;
const MAX_PROPOSAL_RECOVERY_ITEMS = 200;
const MAX_REVIEW_TURN_RECOVERY_ITEMS = 128;

function emptyProposalRecovery(): ProposalRecovery {
  return { operations: [], reviewTurnIds: [] };
}

function isProposalSurface(value: unknown): value is ProposalSurface {
  return typeof value === "string"
    && PROPOSAL_SURFACE_VALUES.includes(value as ProposalSurface);
}

export function proposalOperationFromServer(
  surface: unknown,
  operationId: unknown,
): ProposalOperation | null {
  return isProposalSurface(surface)
    && typeof operationId === "string"
    && UUID_PATTERN.test(operationId)
    ? { surface, operationId }
    : null;
}

function storage(kind: "local" | "session"): Storage | null {
  try {
    if (typeof window === "undefined") return null;
    return kind === "local" ? window.localStorage : window.sessionStorage;
  } catch {
    return null;
  }
}

function remove(area: Storage, key: string): void {
  try {
    area.removeItem(key);
  } catch {
    // Browser storage can be disabled. The in-memory conversation remains usable.
  }
}

function clearUnsafeCrossTabProposalRecovery(): void {
  // Proposal recovery is tab-scoped; obsolete shared-storage markers are not migrated.
  const localArea = storage("local");
  if (localArea !== null) {
    remove(localArea, LEGACY_PROPOSAL_RECOVERY_STORAGE_KEY);
    remove(localArea, PROPOSAL_RECOVERY_STORAGE_KEY);
  }
  const sessionArea = storage("session");
  if (sessionArea !== null) remove(sessionArea, LEGACY_PROPOSAL_RECOVERY_STORAGE_KEY);
}

function validUuidList(value: unknown): string[] | null {
  if (!Array.isArray(value) || value.length > MAX_REVIEW_TURN_RECOVERY_ITEMS) return null;
  const result: string[] = [];
  const seen = new Set<string>();
  for (const item of value) {
    if (typeof item !== "string" || !UUID_PATTERN.test(item)) return null;
    if (seen.has(item)) return null;
    seen.add(item);
    result.push(item);
  }
  return result;
}

function validProposalOperations(value: unknown): ProposalOperation[] | null {
  if (!Array.isArray(value) || value.length > MAX_PROPOSAL_RECOVERY_ITEMS) return null;
  const result: ProposalOperation[] = [];
  const seen = new Set<string>();
  for (const item of value) {
    if (item === null || typeof item !== "object" || Array.isArray(item)) return null;
    const record = item as Record<string, unknown>;
    if (Object.keys(record).some((key) => !["surface", "operation_id"].includes(key))) {
      return null;
    }
    const operation = proposalOperationFromServer(record.surface, record.operation_id);
    if (operation === null) return null;
    const identity = `${operation.surface}:${operation.operationId}`;
    if (seen.has(identity)) return null;
    seen.add(identity);
    result.push(operation);
  }
  return result;
}

export function readProposalRecovery(recoveryScope: string): ProposalRecovery {
  const empty = emptyProposalRecovery();
  clearUnsafeCrossTabProposalRecovery();
  const area = storage("session");
  if (area === null || !SCOPE_PATTERN.test(recoveryScope)) return empty;
  try {
    const raw = area.getItem(PROPOSAL_RECOVERY_STORAGE_KEY);
    if (raw === null) return empty;
    const parsed = JSON.parse(raw) as unknown;
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      remove(area, PROPOSAL_RECOVERY_STORAGE_KEY);
      return empty;
    }
    const record = parsed as Record<string, unknown>;
    const allowedKeys = new Set([
      "version",
      "recovery_scope",
      "operations",
      "review_turn_ids",
    ]);
    const operations = validProposalOperations(record.operations);
    const reviewTurnIds = validUuidList(record.review_turn_ids);
    if (Object.keys(record).some((key) => !allowedKeys.has(key))
        || record.version !== 3
        || record.recovery_scope !== recoveryScope
        || operations === null
        || reviewTurnIds === null) {
      remove(area, PROPOSAL_RECOVERY_STORAGE_KEY);
      return empty;
    }
    return { operations, reviewTurnIds };
  } catch {
    remove(area, PROPOSAL_RECOVERY_STORAGE_KEY);
    return empty;
  }
}

function writeProposalRecovery(recoveryScope: string, recovery: ProposalRecovery): boolean {
  clearUnsafeCrossTabProposalRecovery();
  const area = storage("session");
  if (area === null || !SCOPE_PATTERN.test(recoveryScope)) return false;
  const operations = validProposalOperations(
    recovery.operations.slice(-MAX_PROPOSAL_RECOVERY_ITEMS).map((operation) => ({
      surface: operation.surface,
      operation_id: operation.operationId,
    })),
  );
  const reviewTurnIds = validUuidList(
    recovery.reviewTurnIds.slice(-MAX_REVIEW_TURN_RECOVERY_ITEMS),
  );
  if (operations === null || reviewTurnIds === null) return false;
  if (operations.length === 0 && reviewTurnIds.length === 0) {
    remove(area, PROPOSAL_RECOVERY_STORAGE_KEY);
    return area.getItem(PROPOSAL_RECOVERY_STORAGE_KEY) === null;
  }
  const raw = JSON.stringify({
    version: 3,
    recovery_scope: recoveryScope,
    operations: operations.map((operation) => ({
      surface: operation.surface,
      operation_id: operation.operationId,
    })),
    review_turn_ids: reviewTurnIds,
  });
  try {
    area.setItem(PROPOSAL_RECOVERY_STORAGE_KEY, raw);
    return area.getItem(PROPOSAL_RECOVERY_STORAGE_KEY) === raw;
  } catch {
    return false;
  }
}

function sameProposalOperation(
  left: ProposalOperation,
  right: ProposalOperation,
): boolean {
  return left.surface === right.surface && left.operationId === right.operationId;
}

export function readSettledProposalOperations(recoveryScope: string): ProposalOperation[] {
  const area = storage("session");
  if (area === null || !SCOPE_PATTERN.test(recoveryScope)) return [];
  try {
    const raw = area.getItem(SETTLED_PROPOSALS_STORAGE_KEY);
    if (raw === null) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      remove(area, SETTLED_PROPOSALS_STORAGE_KEY);
      return [];
    }
    const record = parsed as Record<string, unknown>;
    const operations = validProposalOperations(record.operations);
    if (Object.keys(record).some(
      (key) => !["version", "recovery_scope", "operations"].includes(key),
    )
        || record.version !== 1
        || record.recovery_scope !== recoveryScope
        || operations === null) {
      remove(area, SETTLED_PROPOSALS_STORAGE_KEY);
      return [];
    }
    return operations;
  } catch {
    remove(area, SETTLED_PROPOSALS_STORAGE_KEY);
    return [];
  }
}

export function rememberSettledProposalOperation(
  recoveryScope: string,
  operation: ProposalOperation,
): ProposalOperation[] {
  const current = readSettledProposalOperations(recoveryScope);
  const validated = proposalOperationFromServer(operation.surface, operation.operationId);
  const area = storage("session");
  if (validated === null || area === null || !SCOPE_PATTERN.test(recoveryScope)) return current;
  const operations = [
    ...current.filter((item) => !sameProposalOperation(item, validated)),
    validated,
  ].slice(-MAX_PROPOSAL_RECOVERY_ITEMS);
  const raw = JSON.stringify({
    version: 1,
    recovery_scope: recoveryScope,
    operations: operations.map((item) => ({
      surface: item.surface,
      operation_id: item.operationId,
    })),
  });
  try {
    area.setItem(SETTLED_PROPOSALS_STORAGE_KEY, raw);
    return area.getItem(SETTLED_PROPOSALS_STORAGE_KEY) === raw ? operations : current;
  } catch {
    return current;
  }
}

export function rememberProposalOperation(
  recoveryScope: string,
  operation: ProposalOperation,
): ProposalRecovery {
  const current = readProposalRecovery(recoveryScope);
  const validated = proposalOperationFromServer(operation.surface, operation.operationId);
  if (validated === null) return current;
  current.operations = [
    ...current.operations.filter((item) => !sameProposalOperation(item, validated)),
    validated,
  ].slice(-MAX_PROPOSAL_RECOVERY_ITEMS);
  writeProposalRecovery(recoveryScope, current);
  return current;
}

export function forgetProposalOperation(
  recoveryScope: string,
  operation: ProposalOperation,
): ProposalRecovery {
  const current = readProposalRecovery(recoveryScope);
  const validated = proposalOperationFromServer(operation.surface, operation.operationId);
  if (validated === null) return current;
  current.operations = current.operations.filter(
    (item) => !sameProposalOperation(item, validated),
  );
  writeProposalRecovery(recoveryScope, current);
  return current;
}

export function rememberReviewProposalTurn(
  recoveryScope: string,
  clientTurnId: string,
): ProposalRecovery {
  const current = readProposalRecovery(recoveryScope);
  if (!UUID_PATTERN.test(clientTurnId)) return current;
  current.reviewTurnIds = [
    ...current.reviewTurnIds.filter((item) => item !== clientTurnId),
    clientTurnId,
  ].slice(-MAX_REVIEW_TURN_RECOVERY_ITEMS);
  writeProposalRecovery(recoveryScope, current);
  return current;
}

export function forgetReviewProposalTurn(
  recoveryScope: string,
  clientTurnId: string,
): ProposalRecovery {
  const current = readProposalRecovery(recoveryScope);
  if (!UUID_PATTERN.test(clientTurnId)) return current;
  current.reviewTurnIds = current.reviewTurnIds.filter((item) => item !== clientTurnId);
  writeProposalRecovery(recoveryScope, current);
  return current;
}

export function clearProposalRecovery(recoveryScope: string): boolean {
  clearUnsafeCrossTabProposalRecovery();
  const area = storage("session");
  if (area === null || !SCOPE_PATTERN.test(recoveryScope)) return false;
  remove(area, PROPOSAL_RECOVERY_STORAGE_KEY);
  return area.getItem(PROPOSAL_RECOVERY_STORAGE_KEY) === null;
}

export function readVisibleTrustedOperationTurns(recoveryScope: string): string[] {
  const area = storage("session");
  if (area === null || !SCOPE_PATTERN.test(recoveryScope)) return [];
  try {
    const raw = area.getItem(VISIBLE_TURNS_STORAGE_KEY);
    if (raw === null) return [];
    const parsed = JSON.parse(raw) as unknown;
    if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) {
      remove(area, VISIBLE_TURNS_STORAGE_KEY);
      return [];
    }
    const record = parsed as Record<string, unknown>;
    const ids = validUuidList(record.turn_ids);
    if (Object.keys(record).some((key) => !["version", "recovery_scope", "turn_ids"].includes(key))
        || record.version !== 1
        || record.recovery_scope !== recoveryScope
        || ids === null) {
      remove(area, VISIBLE_TURNS_STORAGE_KEY);
      return [];
    }
    return ids;
  } catch {
    remove(area, VISIBLE_TURNS_STORAGE_KEY);
    return [];
  }
}

export function storeVisibleTrustedOperationTurns(
  recoveryScope: string,
  turnIds: string[],
): boolean {
  const area = storage("session");
  if (area === null || !SCOPE_PATTERN.test(recoveryScope)) return false;
  const ids = validUuidList(turnIds.slice(-MAX_REVIEW_TURN_RECOVERY_ITEMS));
  if (ids === null) return false;
  if (ids.length === 0) {
    remove(area, VISIBLE_TURNS_STORAGE_KEY);
    return area.getItem(VISIBLE_TURNS_STORAGE_KEY) === null;
  }
  const raw = JSON.stringify({
    version: 1,
    recovery_scope: recoveryScope,
    turn_ids: ids,
  });
  try {
    area.setItem(VISIBLE_TURNS_STORAGE_KEY, raw);
    return area.getItem(VISIBLE_TURNS_STORAGE_KEY) === raw;
  } catch {
    return false;
  }
}
