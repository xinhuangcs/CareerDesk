import type { ApplicationUpdateOperation } from "./applicationUpdateOperationContract";
import { applicationUpdateOperationIntegrityIssue } from "./applicationUpdateOperationContract";
import type { UiLocale } from "../../i18n/i18n.ts";

export function applicationUpdateOperationAnnouncement(
  operation: ApplicationUpdateOperation,
  actionUncertain: boolean,
  locale: UiLocale = "zh-CN",
): string {
  const en = locale === "en";
  if (actionUncertain) return en
    ? `Undo for role ${operation.target.application_id} needs verification`
    : `岗位 ${operation.target.application_id} 的撤销结果待确认`;
  if (applicationUpdateOperationIntegrityIssue(operation) !== null) {
    return en
      ? `Update for role ${operation.target.application_id} needs verification`
      : `岗位 ${operation.target.application_id} 的修改结果异常，需要核对`;
  }
  if (operation.state === "completed") {
    return en
      ? `Updated ${operation.final.company} · ${operation.final.position}`
      : `岗位 ${operation.final.company} ${operation.final.position} 已修改`;
  }
  if (operation.state === "undone") {
    return en
      ? `Undid the update to ${operation.before.company} · ${operation.before.position}`
      : `岗位 ${operation.before.company} ${operation.before.position} 的修改已撤销`;
  }
  return en
    ? `Update status for role ${operation.target.application_id} needs verification`
    : `岗位 ${operation.target.application_id} 的修改状态需要核对`;
}
