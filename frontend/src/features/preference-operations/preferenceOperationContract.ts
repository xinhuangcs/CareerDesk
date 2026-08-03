export type PreferenceOperationAction = "set" | "delete";
export type PreferenceOperationOutcome =
  | "created"
  | "updated"
  | "deleted"
  | "unchanged"
  | "missing";

export type PreferenceOperationEffect = {
  action: PreferenceOperationAction;
  key: string;
  outcome: PreferenceOperationOutcome;
  before_id: number | null;
  before_revision: number | null;
  final_id: number | null;
  final_revision: number | null;
  current: boolean;
};

export type PreferenceOperationResult = {
  requested_count: number;
  changed_count: number;
  unchanged_count: number;
  created_count: number;
  updated_count: number;
  deleted_count: number;
  missing_count: number;
};

export type PreferenceUpdateOperation = {
  operation_id: string;
  operation_type: "preference_update";
  contract_version: 1;
  state: "completed";
  created_time: string;
  client_turn_id: string;
  effects: PreferenceOperationEffect[];
  current: boolean;
  result: PreferenceOperationResult;
};

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const VALID_ACTIONS = new Set<PreferenceOperationAction>(["set", "delete"]);
const VALID_OUTCOMES = new Set<PreferenceOperationOutcome>([
  "created", "updated", "deleted", "unchanged", "missing",
]);
const MAX_EFFECTS = 20;
const MAX_KEY_LENGTH = 100;
const UTC_ISO_TIME_PATTERN = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{6}))?\+00:00$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function hasExactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const actual = Object.keys(value);
  return actual.length === keys.length && keys.every((key) => actual.includes(key));
}

function codePointLength(value: string): number {
  return Array.from(value).length;
}

function compareCodePoints(left: string, right: string): number {
  const leftPoints = Array.from(left, (item) => item.codePointAt(0) ?? 0);
  const rightPoints = Array.from(right, (item) => item.codePointAt(0) ?? 0);
  const sharedLength = Math.min(leftPoints.length, rightPoints.length);
  for (let index = 0; index < sharedLength; index += 1) {
    if (leftPoints[index] !== rightPoints[index]) return leftPoints[index] - rightPoints[index];
  }
  return leftPoints.length - rightPoints.length;
}

function isBoundedTrimmedString(value: unknown, maxLength: number): value is string {
  return typeof value === "string"
    && value.trim() === value
    && codePointLength(value) >= 1
    && codePointLength(value) <= maxLength;
}

function isUuid(value: unknown): value is string {
  return typeof value === "string" && UUID_PATTERN.test(value);
}

function isPositiveInteger(value: unknown): value is number {
  return Number.isSafeInteger(value) && (value as number) > 0;
}

function isBoundedCount(value: unknown): value is number {
  return Number.isSafeInteger(value) && (value as number) >= 0 && (value as number) <= MAX_EFFECTS;
}

function isCreatedTime(value: unknown): value is string {
  if (!isBoundedTrimmedString(value, 64)) return false;
  const match = UTC_ISO_TIME_PATTERN.exec(value);
  if (match === null || match[1] === "0000" || match[7] === "000000") return false;
  const millisecond = match[7]?.slice(0, 3) ?? "000";
  const millisecondIso = `${match[1]}-${match[2]}-${match[3]}T${match[4]}:${match[5]}:${match[6]}.${millisecond}Z`;
  const parsed = new Date(millisecondIso);
  return Number.isFinite(parsed.getTime()) && parsed.toISOString() === millisecondIso;
}

function isNullablePositiveInteger(value: unknown): value is number | null {
  return value === null || isPositiveInteger(value);
}

function isPreferenceOperationEffect(value: unknown): value is PreferenceOperationEffect {
  return isRecord(value)
    && hasExactKeys(value, [
      "action",
      "key",
      "outcome",
      "before_id",
      "before_revision",
      "final_id",
      "final_revision",
      "current",
    ])
    && typeof value.action === "string"
    && VALID_ACTIONS.has(value.action as PreferenceOperationAction)
    && isBoundedTrimmedString(value.key, MAX_KEY_LENGTH)
    && typeof value.outcome === "string"
    && VALID_OUTCOMES.has(value.outcome as PreferenceOperationOutcome)
    && isNullablePositiveInteger(value.before_id)
    && isNullablePositiveInteger(value.before_revision)
    && isNullablePositiveInteger(value.final_id)
    && isNullablePositiveInteger(value.final_revision)
    && typeof value.current === "boolean";
}

function isPreferenceOperationResult(value: unknown): value is PreferenceOperationResult {
  return isRecord(value)
    && hasExactKeys(value, [
      "requested_count",
      "changed_count",
      "unchanged_count",
      "created_count",
      "updated_count",
      "deleted_count",
      "missing_count",
    ])
    && isBoundedCount(value.requested_count)
    && isBoundedCount(value.changed_count)
    && isBoundedCount(value.unchanged_count)
    && isBoundedCount(value.created_count)
    && isBoundedCount(value.updated_count)
    && isBoundedCount(value.deleted_count)
    && isBoundedCount(value.missing_count);
}

function effectIdentityIssue(effect: PreferenceOperationEffect): string | null {
  switch (effect.outcome) {
    case "created":
      if (effect.action !== "set") return "created preference does not use the set action";
      if (effect.before_id !== null || effect.before_revision !== null
          || effect.final_id === null || effect.final_revision !== 1) {
        return "created preference has inconsistent row identity or revision";
      }
      return null;
    case "updated":
      if (effect.action !== "set") return "updated preference does not use the set action";
      if (effect.before_id === null || effect.before_revision === null
          || effect.final_id !== effect.before_id
          || effect.final_revision !== effect.before_revision + 1) {
        return "updated preference has discontinuous row identity or revision";
      }
      return null;
    case "unchanged":
      if (effect.action !== "set") return "unchanged preference does not use the set action";
      if (effect.before_id === null || effect.before_revision === null
          || effect.final_id !== effect.before_id
          || effect.final_revision !== effect.before_revision) {
        return "unchanged preference has inconsistent before and after identity or revision";
      }
      return null;
    case "deleted":
      if (effect.action !== "delete") return "deleted preference does not use the delete action";
      if (effect.before_id === null || effect.before_revision === null
          || effect.final_id !== null || effect.final_revision !== null) {
        return "deleted preference has inconsistent before and after identity or revision";
      }
      return null;
    case "missing":
      if (effect.action !== "delete") return "missing deletion does not use the delete action";
      if (effect.before_id !== null || effect.before_revision !== null
          || effect.final_id !== null || effect.final_revision !== null) {
        return "missing deletion carries a row identity or revision";
      }
      return null;
    default:
      return assertNever(effect.outcome);
  }
}

function assertNever(value: never): never {
  throw new Error(`Unhandled preference operation outcome: ${String(value)}`);
}

export function preferenceOperationIntegrityIssue(
  operation: PreferenceUpdateOperation,
): string | null {
  const keys = new Set<string>();
  const counts: Record<PreferenceOperationOutcome, number> = {
    created: 0,
    updated: 0,
    deleted: 0,
    unchanged: 0,
    missing: 0,
  };
  let previousKey: string | null = null;
  for (const effect of operation.effects) {
    if (keys.has(effect.key)) return `operation processes preference ${effect.key} more than once`;
    if (previousKey !== null && compareCodePoints(previousKey, effect.key) >= 0) {
      return "preference effects are not sorted by canonical key";
    }
    keys.add(effect.key);
    previousKey = effect.key;
    const identityIssue = effectIdentityIssue(effect);
    if (identityIssue !== null) return identityIssue;
    counts[effect.outcome] += 1;
  }
  const result = operation.result;
  if (result.requested_count !== operation.effects.length
      || result.created_count !== counts.created
      || result.updated_count !== counts.updated
      || result.deleted_count !== counts.deleted
      || result.unchanged_count !== counts.unchanged
      || result.missing_count !== counts.missing) {
    return "summary counts do not match item outcomes";
  }
  if (result.changed_count !== counts.created + counts.updated + counts.deleted) {
    return "changed count does not match item outcomes";
  }
  if (result.changed_count < 1) return "a request with no changes must not create an operation receipt";
  if (result.requested_count !== result.changed_count
      + result.unchanged_count + result.missing_count) {
    return "request count does not balance with outcome counts";
  }
  if (operation.current !== operation.effects.every((effect) => effect.current)) {
    return "operation currentness does not match item currentness";
  }
  return null;
}

export function isPreferenceUpdateOperation(value: unknown): value is PreferenceUpdateOperation {
  if (!isRecord(value) || !hasExactKeys(value, [
    "operation_id",
    "operation_type",
    "contract_version",
    "state",
    "created_time",
    "client_turn_id",
    "effects",
    "current",
    "result",
  ]) || !isUuid(value.operation_id)
      || value.operation_type !== "preference_update"
      || value.contract_version !== 1
      || value.state !== "completed"
      || !isCreatedTime(value.created_time)
      || !isUuid(value.client_turn_id)
      || !Array.isArray(value.effects)
      || value.effects.length < 1
      || value.effects.length > MAX_EFFECTS
      || !value.effects.every(isPreferenceOperationEffect)
      || typeof value.current !== "boolean"
      || !isPreferenceOperationResult(value.result)) return false;
  return preferenceOperationIntegrityIssue(value as PreferenceUpdateOperation) === null;
}
