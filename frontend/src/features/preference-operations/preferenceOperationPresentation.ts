import type { PreferenceOperationOutcome, PreferenceUpdateOperation } from "./preferenceOperationContract";
import { preferenceOperationIntegrityIssue } from "./preferenceOperationContract";
import type { UiLocale } from "../../i18n/i18n";

export const PREFERENCE_OUTCOME_LABELS: Record<PreferenceOperationOutcome, string> = {
  created: "已新增",
  updated: "已更新",
  deleted: "已删除",
  unchanged: "无需变更",
  missing: "未找到，未删除",
};

export function preferenceOperationAnnouncement(
  operation: PreferenceUpdateOperation,
  locale: UiLocale = "zh-CN",
): string {
  const en = locale === "en";
  if (preferenceOperationIntegrityIssue(operation) !== null) {
    return en ? "Preference changes need verification" : "偏好修改结果异常，需要核对";
  }
  if (!operation.current) {
    return en
      ? `Historical preference operation processed ${operation.result.requested_count} items`
      : `偏好历史操作共处理 ${operation.result.requested_count} 项`;
  }
  if (operation.result.changed_count === 0) {
    return en
      ? `Checked ${operation.result.requested_count} preferences; no changes needed`
      : `偏好已核对，共 ${operation.result.requested_count} 项，无需变更`;
  }
  return en
    ? `Updated ${operation.result.changed_count} preferences`
    : `偏好已更新 ${operation.result.changed_count} 项`;
}
