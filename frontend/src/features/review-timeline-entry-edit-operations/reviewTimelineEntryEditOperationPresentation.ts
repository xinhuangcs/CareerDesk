import type { ReviewTimelineEntryEditOperation } from "./reviewTimelineEntryEditOperationContract";
import {
  reviewTimelineEntryEditOperationIntegrityIssue,
} from "./reviewTimelineEntryEditOperationContract";
import type { UiLocale } from "../../i18n/i18n.ts";

export function reviewTimelineEntryEditOperationAnnouncement(
  operation: ReviewTimelineEntryEditOperation,
  actionUncertain: boolean,
  locale: UiLocale = "zh-CN",
): string {
  const en = locale === "en";
  if (actionUncertain) return en
    ? `Undo for review ${operation.target.journal_id} needs verification`
    : `复盘 ${operation.target.journal_id} 的撤销结果待确认`;
  if (reviewTimelineEntryEditOperationIntegrityIssue(operation) !== null) {
    return en
      ? `History edit for review ${operation.target.journal_id} needs verification`
      : `复盘 ${operation.target.journal_id} 的历程编辑结果异常，需要核对`;
  }
  if (operation.state === "completed") {
    return en
      ? `Edited review history for ${operation.target.company} · ${operation.target.position}`
      : `${operation.target.company} ${operation.target.position} 的复盘历程已编辑`;
  }
  if (operation.state === "undone") {
    return en
      ? `Undid the history edit for review ${operation.target.journal_id}`
      : `复盘 ${operation.target.journal_id} 的历程编辑已撤销`;
  }
  return en
    ? `History-edit status for review ${operation.target.journal_id} needs verification`
    : `复盘 ${operation.target.journal_id} 的历程编辑状态需要核对`;
}
