import { isCanonicalPreferenceTime } from "./preferencesContract.ts";

export type PreferenceItemCommandAction = "set" | "delete";
export type PreferenceItemCommandTarget = { id: number; revision: number };
export type PreferenceItemCommandSkeleton = {
  action: PreferenceItemCommandAction;
  target: PreferenceItemCommandTarget;
};
export type PreferenceItemCommandPayload =
  | { action: "set"; target: PreferenceItemCommandTarget; value: string }
  | { action: "delete"; target: PreferenceItemCommandTarget };

export type PreferenceItemCommandStatus = {
  contract_version: 1;
  command_id: string;
  state: "completed" | "rejected" | "cancelled";
  action: PreferenceItemCommandAction;
  target: PreferenceItemCommandTarget;
  result: {
    outcome: "updated" | "deleted" | "no_change";
    before: PreferenceItemCommandTarget;
    final: PreferenceItemCommandTarget | null;
  } | null;
  error: {
    code: "target_missing" | "target_changed" | "limit_exceeded" | "projection_invalid";
    message: string;
  } | null;
  operation_id: string | null;
  finished_time: string;
};

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const ERROR_CODES = new Set([
  "target_missing", "target_changed", "limit_exceeded", "projection_invalid",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function hasExactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const actual = Object.keys(value);
  return actual.length === keys.length && keys.every((key) => actual.includes(key));
}

function isPositiveInteger(value: unknown): value is number {
  return Number.isSafeInteger(value) && (value as number) > 0;
}

export function isPreferenceItemCommandTarget(
  value: unknown,
): value is PreferenceItemCommandTarget {
  return isRecord(value)
    && hasExactKeys(value, ["id", "revision"])
    && isPositiveInteger(value.id)
    && isPositiveInteger(value.revision);
}

export function samePreferenceItemCommandTarget(
  left: PreferenceItemCommandTarget,
  right: PreferenceItemCommandTarget,
): boolean {
  return left.id === right.id && left.revision === right.revision;
}

export function isPreferenceItemCommandStatus(
  value: unknown,
  commandId: string,
  expected: PreferenceItemCommandSkeleton,
): value is PreferenceItemCommandStatus {
  if (!isRecord(value)
      || !hasExactKeys(value, [
        "contract_version", "command_id", "state", "action", "target", "result",
        "error", "operation_id", "finished_time",
      ])
      || value.contract_version !== 1
      || value.command_id !== commandId
      || !UUID_PATTERN.test(commandId)
      || value.action !== expected.action
      || !isPreferenceItemCommandTarget(value.target)
      || !samePreferenceItemCommandTarget(value.target, expected.target)
      || !isCanonicalPreferenceTime(value.finished_time)) return false;

  if (value.state === "cancelled") {
    return value.result === null && value.error === null && value.operation_id === null;
  }
  if (value.state === "rejected") {
    return value.result === null
      && value.operation_id === null
      && isRecord(value.error)
      && hasExactKeys(value.error, ["code", "message"])
      && typeof value.error.code === "string"
      && ERROR_CODES.has(value.error.code)
      && typeof value.error.message === "string"
      && value.error.message.trim() === value.error.message
      && Array.from(value.error.message).length >= 1
      && Array.from(value.error.message).length <= 256;
  }
  if (value.state !== "completed" || value.error !== null || !isRecord(value.result)
      || !hasExactKeys(value.result, ["outcome", "before", "final"])
      || !isPreferenceItemCommandTarget(value.result.before)
      || !samePreferenceItemCommandTarget(value.result.before, expected.target)) return false;
  const final = value.result.final;
  if (value.result.outcome === "updated") {
    return expected.action === "set"
      && typeof value.operation_id === "string"
      && UUID_PATTERN.test(value.operation_id)
      && isPreferenceItemCommandTarget(final)
      && final.id === expected.target.id
      && final.revision === expected.target.revision + 1;
  }
  if (value.result.outcome === "deleted") {
    return expected.action === "delete"
      && typeof value.operation_id === "string"
      && UUID_PATTERN.test(value.operation_id)
      && final === null;
  }
  return expected.action === "set"
    && value.result.outcome === "no_change"
    && value.operation_id === null
    && isPreferenceItemCommandTarget(final)
    && samePreferenceItemCommandTarget(final, expected.target);
}
