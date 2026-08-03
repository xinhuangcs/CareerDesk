import {
  isPreferenceItemCommandTarget,
  type PreferenceItemCommandAction,
  type PreferenceItemCommandSkeleton,
} from "./preferenceItemCommandContract.ts";

const STORAGE_KEY_PREFIX = "careerdesk_preference_item_command_outbox_v1_";
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const SCOPE_PATTERN = /^[0-9a-f]{64}$/;

export type PersistedPreferenceItemCommand = PreferenceItemCommandSkeleton & {
  commandId: string;
  createdAt: number;
};

export type PreferenceItemCommandOutboxRead =
  | { state: "empty" }
  | { state: "pending"; command: PersistedPreferenceItemCommand }
  | { state: "corrupt" }
  | { state: "unavailable" };

function storage(): Storage | null {
  try {
    return typeof window === "undefined" ? null : window.sessionStorage;
  } catch {
    return null;
  }
}

function exactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const actual = Object.keys(value);
  return actual.length === keys.length && keys.every((key) => actual.includes(key));
}

function storageKey(recoveryScope: string): string {
  return `${STORAGE_KEY_PREFIX}${recoveryScope}`;
}

function remove(area: Storage, recoveryScope: string): boolean {
  const key = storageKey(recoveryScope);
  try {
    area.removeItem(key);
    return area.getItem(key) === null;
  } catch {
    return false;
  }
}

function parse(raw: string, recoveryScope: string): PersistedPreferenceItemCommand | null {
  try {
    const value = JSON.parse(raw) as unknown;
    if (value === null || typeof value !== "object" || Array.isArray(value)) return null;
    const record = value as Record<string, unknown>;
    if (!exactKeys(record, [
      "version", "recovery_scope", "command_id", "action", "target", "created_at",
    ])
        || record.version !== 1
        || record.recovery_scope !== recoveryScope
        || !SCOPE_PATTERN.test(recoveryScope)
        || typeof record.command_id !== "string"
        || !UUID_PATTERN.test(record.command_id)
        || (record.action !== "set" && record.action !== "delete")
        || !isPreferenceItemCommandTarget(record.target)
        || typeof record.created_at !== "number"
        || !Number.isFinite(record.created_at)
        || record.created_at < 0
        || record.created_at > Date.now() + 60_000) return null;
    return {
      commandId: record.command_id,
      action: record.action as PreferenceItemCommandAction,
      target: record.target,
      createdAt: record.created_at,
    };
  } catch {
    return null;
  }
}

export function readPreferenceItemCommandOutbox(
  recoveryScope: string,
): PreferenceItemCommandOutboxRead {
  const area = storage();
  if (area === null || !SCOPE_PATTERN.test(recoveryScope)) return { state: "unavailable" };
  const key = storageKey(recoveryScope);
  try {
    const raw = area.getItem(key);
    if (raw === null) return { state: "empty" };
    const parsed = parse(raw, recoveryScope);
    if (parsed !== null) return { state: "pending", command: parsed };
    remove(area, recoveryScope);
    return { state: "corrupt" };
  } catch {
    return { state: "unavailable" };
  }
}

export function persistPreferenceItemCommand(
  recoveryScope: string,
  command: PersistedPreferenceItemCommand,
): boolean {
  const area = storage();
  if (area === null || !SCOPE_PATTERN.test(recoveryScope)) return false;
  const key = storageKey(recoveryScope);
  const existing = readPreferenceItemCommandOutbox(recoveryScope);
  if (existing.state === "pending" && existing.command.commandId !== command.commandId) return false;
  const raw = JSON.stringify({
    version: 1,
    recovery_scope: recoveryScope,
    command_id: command.commandId,
    action: command.action,
    target: command.target,
    created_at: command.createdAt,
  });
  try {
    area.setItem(key, raw);
    if (area.getItem(key) !== raw) return false;
    const verified = readPreferenceItemCommandOutbox(recoveryScope);
    return verified.state === "pending"
      && verified.command.commandId === command.commandId
      && verified.command.action === command.action
      && verified.command.target.id === command.target.id
      && verified.command.target.revision === command.target.revision;
  } catch {
    return false;
  }
}

export function clearPreferenceItemCommandOutbox(
  recoveryScope: string,
  commandId: string,
): boolean {
  const area = storage();
  if (area === null) return false;
  const existing = readPreferenceItemCommandOutbox(recoveryScope);
  if (existing.state === "empty") return true;
  if (existing.state !== "pending" || existing.command.commandId !== commandId) return false;
  return remove(area, recoveryScope);
}
