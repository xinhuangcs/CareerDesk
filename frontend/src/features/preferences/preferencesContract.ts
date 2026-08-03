export type PreferenceItem = {
  id: number;
  key: string;
  value: string;
  revision: number;
  created_time: string;
  updated_time: string;
};

export type PreferencesSnapshot = {
  items: PreferenceItem[];
  total: number;
  total_chars: number;
  recovery_scope: string;
};

const MAX_PREFERENCES = 100;
const MAX_KEY_LENGTH = 100;
const MAX_VALUE_LENGTH = 2_000;
const MAX_TOTAL_CHARS = 20_000;
const RECOVERY_SCOPE_PATTERN = /^[0-9a-f]{64}$/;
const UTC_ISO_TIME_PATTERN = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2}):(\d{2})(?:\.(\d{6}))?\+00:00$/;

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function hasExactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const actual = Object.keys(value);
  return actual.length === keys.length && keys.every((key) => actual.includes(key));
}

function codePoints(value: string): number[] {
  return Array.from(value, (item) => item.codePointAt(0) ?? 0);
}

function codePointLength(value: string): number {
  return codePoints(value).length;
}

function compareCodePoints(left: string, right: string): number {
  const leftPoints = codePoints(left);
  const rightPoints = codePoints(right);
  const sharedLength = Math.min(leftPoints.length, rightPoints.length);
  for (let index = 0; index < sharedLength; index += 1) {
    if (leftPoints[index] !== rightPoints[index]) return leftPoints[index] - rightPoints[index];
  }
  return leftPoints.length - rightPoints.length;
}

function isBoundedString(value: unknown, maxLength: number): value is string {
  return typeof value === "string"
    && value.trim() === value
    && codePointLength(value) >= 1
    && codePointLength(value) <= maxLength;
}

export function isCanonicalPreferenceTime(value: unknown): value is string {
  if (typeof value !== "string"
      || value.trim() !== value
      || codePointLength(value) < 1
      || codePointLength(value) > 64) return false;
  const match = UTC_ISO_TIME_PATTERN.exec(value);
  if (match === null || match[1] === "0000" || match[7] === "000000") return false;
  const millisecond = match[7]?.slice(0, 3) ?? "000";
  const millisecondIso = `${match[1]}-${match[2]}-${match[3]}T${match[4]}:${match[5]}:${match[6]}.${millisecond}Z`;
  const parsed = new Date(millisecondIso);
  return Number.isFinite(parsed.getTime()) && parsed.toISOString() === millisecondIso;
}

function isPositiveInteger(value: unknown): value is number {
  return Number.isSafeInteger(value) && (value as number) > 0;
}

function isNonNegativeIntegerAtMost(value: unknown, maximum: number): value is number {
  return Number.isSafeInteger(value) && (value as number) >= 0 && (value as number) <= maximum;
}

function isPreferenceItem(value: unknown): value is PreferenceItem {
  return isRecord(value)
    && hasExactKeys(value, ["id", "key", "value", "revision", "created_time", "updated_time"])
    && isPositiveInteger(value.id)
    && isBoundedString(value.key, MAX_KEY_LENGTH)
    && isBoundedString(value.value, MAX_VALUE_LENGTH)
    && isPositiveInteger(value.revision)
    && isCanonicalPreferenceTime(value.created_time)
    && isCanonicalPreferenceTime(value.updated_time);
}

export function isPreferencesSnapshot(value: unknown): value is PreferencesSnapshot {
  if (!isRecord(value)
      || !hasExactKeys(value, ["items", "total", "total_chars", "recovery_scope"])
      || !Array.isArray(value.items)
      || value.items.length > MAX_PREFERENCES
      || !isNonNegativeIntegerAtMost(value.total, MAX_PREFERENCES)
      || value.total !== value.items.length
      || !isNonNegativeIntegerAtMost(value.total_chars, MAX_TOTAL_CHARS)
      || typeof value.recovery_scope !== "string"
      || !RECOVERY_SCOPE_PATTERN.test(value.recovery_scope)) return false;

  let totalChars = 0;
  let previousKey: string | null = null;
  const ids = new Set<number>();
  for (const item of value.items) {
    if (!isPreferenceItem(item)) return false;
    if (ids.has(item.id)) return false;
    ids.add(item.id);
    if (previousKey !== null && compareCodePoints(previousKey, item.key) >= 0) return false;
    previousKey = item.key;
    totalChars += codePointLength(item.key) + codePointLength(item.value);
  }
  return value.total_chars === totalChars;
}
