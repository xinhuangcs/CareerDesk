import type { PreferenceItem, PreferencesSnapshot } from "./preferencesContract.ts";
import type { PreferenceItemCommandStatus } from "./preferenceItemCommandContract.ts";
import type { UiLocale } from "../../i18n/i18n.ts";

export const PREFERENCE_COMMAND_RETRY_DELAYS_MS = [1_000, 2_000, 4_000, 8_000, 15_000, 30_000] as const;

export function preferenceValueCodePointLength(value: string): number {
  return Array.from(value).length;
}

export function preferenceValueValidationIssue(value: string, current: string, locale: UiLocale = "zh-CN"): string | null {
  const l = (zh: string, en: string) => locale === "en" ? en : zh;
  const length = preferenceValueCodePointLength(value);
  if (length < 1) return l("偏好值不能为空。", "The preference value cannot be empty.");
  if (length > 2_000) return l("偏好值最多 2000 个字符。", "The preference value cannot exceed 2,000 characters.");
  if (value.trim() !== value) return l("请删除偏好值首尾的空白字符。", "Remove whitespace at the beginning or end of the preference value.");
  if (value === current) return l("内容没有变化。", "The content has not changed.");
  return null;
}

function itemById(snapshot: PreferencesSnapshot, id: number): PreferenceItem | null {
  return snapshot.items.find((item) => item.id === id) ?? null;
}

export type PreferenceCommandReconciliation = {
  valid: boolean;
  current: boolean;
  message: string;
};

export function reconcilePreferenceItemCommand(
  status: PreferenceItemCommandStatus,
  snapshot: PreferencesSnapshot,
  locale: UiLocale = "zh-CN",
): PreferenceCommandReconciliation {
  const l = (zh: string, en: string) => locale === "en" ? en : zh;
  const item = itemById(snapshot, status.target.id);
  if (status.state === "cancelled") {
    return { valid: true, current: true, message: l("该请求已停止，没有更改这项偏好。", "The request was stopped without changing this preference.") };
  }
  if (status.state === "rejected") {
    return {
      valid: true,
      current: true,
      message: status.error?.message ?? l("偏好没有修改。", "The preference was not changed."),
    };
  }
  const result = status.result;
  if (result === null) return { valid: false, current: false, message: l("操作结果不完整，请刷新后重试。", "The operation result is incomplete. Refresh and try again.") };
  if (result.outcome === "deleted") {
    if (item !== null) return { valid: false, current: false, message: l("删除结果与当前偏好不一致，请刷新后重试。", "The deletion result does not match the current preference. Refresh and try again.") };
    return { valid: true, current: true, message: l("原偏好已删除，页面显示的是最新列表。", "The original preference was deleted and the page shows the latest list.") };
  }
  const final = result.final;
  if (final === null) return { valid: false, current: false, message: l("更新结果不完整，请刷新后重试。", "The update result is incomplete. Refresh and try again.") };
  if (item === null) {
    return {
      valid: true,
      current: false,
      message: l("修改曾经完成，但这项偏好后来已被删除。", "The change completed, but this preference was later deleted."),
    };
  }
  if (item.revision < final.revision) {
    return { valid: false, current: false, message: l("页面版本落后于最新修改，请刷新后重试。", "This page is behind the latest change. Refresh and try again.") };
  }
  if (item.revision > final.revision) {
    return {
      valid: true,
      current: false,
      message: l("修改曾经完成，但这项偏好后来又被更新；页面显示的是当前值。", "The change completed, but the preference was updated again later. The page shows its current value."),
    };
  }
  return {
    valid: true,
    current: true,
    message: result.outcome === "no_change" ? l("内容没有变化。", "The content has not changed.") : l("偏好已更新。", "Preference updated."),
  };
}
