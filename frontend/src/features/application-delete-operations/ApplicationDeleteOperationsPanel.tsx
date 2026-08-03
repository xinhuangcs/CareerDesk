import { useMemo } from "react";

import { formatDate } from "../../i18n/formatters";
import type { UiLocale } from "../../i18n/i18n";
import { useLocale } from "../../i18n/localePreference";
import { useLocalizer } from "../../i18n/useLocalizer";
import {
  approveApplicationDeleteOperation,
  getApplicationDeleteOperation,
  getPendingApplicationDeleteOperations,
  rejectApplicationDeleteOperation,
} from "./applicationDeleteOperationApi";
import type { ApplicationDeleteOperation } from "./applicationDeleteOperationContract";
import { BinaryProposalPanelShell } from "../operations/BinaryProposalPanelShell";
import { type OperationAnchorIdResolver } from "../operations/operationAnchorPortal";
import { ProposalDecisionCard } from "../operations/ProposalDecisionCard";
import {
  type BinaryTrustedOperationAction,
  type BinaryTrustedOperationMessages,
  type BinaryTrustedOperationNoticePayload,
} from "../operations/useBinaryTrustedOperationQueue";

type ApplicationDeleteAction = BinaryTrustedOperationAction;

const STAGE_LABELS_ZH: Record<string, string> = {
  backlog: "待定",
  applied: "已投递",
  written_test: "笔试中",
  interviewing: "面试中",
  offer: "Offer",
  withdrawn: "不再跟进",
  rejected: "已挂",
  pooled: "泡池子",
};

const STAGE_LABELS_EN: Record<string, string> = {
  backlog: "Considering",
  applied: "Applied",
  written_test: "Assessment",
  interviewing: "Interviewing",
  offer: "Offer",
  withdrawn: "Withdrawn",
  rejected: "Rejected",
  pooled: "On hold",
};

function formatOperationTime(value: string, locale: UiLocale = "zh-CN"): string {
  return formatDate(value, locale, "dateTime");
}

function operationIdentity(operation: ApplicationDeleteOperation, locale: UiLocale = "zh-CN"): string {
  const target = operation.target;
  const time = formatOperationTime(target.application_updated_time, locale)
    || target.application_updated_time;
  return `${target.company} · ${target.position} · ${time}`;
}

function completedMessage(operation: ApplicationDeleteOperation, locale: UiLocale = "zh-CN"): string {
  const result = operation.result;
  if (result === null) {
    return locale === "en"
      ? `${operationIdentity(operation, locale)} appears deleted, but the full scope could not be verified. Refresh Application Tracker to confirm.`
      : `${operationIdentity(operation)} 显示已删除，但删除范围暂时无法完整核对；请刷新求职进展确认。`;
  }
  if (locale === "en") {
    return `${operationIdentity(operation, locale)} was deleted; removed ${result.timeline_entries_removed} history entries, detached ${result.questions_detached} questions, ${result.question_occurrences_detached} question occurrences, and ${result.resumes_detached} role-linked résumés while preserving them as previewed.`;
  }
  return `${operationIdentity(operation)} 已删除；清除 ${result.timeline_entries_removed} 条历程，`
    + `并按预览保留、解绑 ${result.questions_detached} 道题、`
    + `${result.question_occurrences_detached} 条题目出处和 `
    + `${result.resumes_detached} 份与岗位关联的简历。`;
}

function completedResultMatchesPreview(operation: ApplicationDeleteOperation): boolean {
  const { result, target, effect } = operation;
  return result !== null
    && result.status === "ok"
    && result.application_id === target.application_id
    && result.timeline_entries_removed === effect.timeline_entries.length
    && result.questions_detached === effect.questions_detached.length
    && result.question_occurrences_detached === effect.question_occurrences_detached
    && result.resumes_detached === effect.resumes_detached.length;
}

function terminalNotice(
  operation: ApplicationDeleteOperation,
  locale: UiLocale = "zh-CN",
): BinaryTrustedOperationNoticePayload {
  const identity = operationIdentity(operation, locale);
  if (operation.state === "completed") {
    if (!completedResultMatchesPreview(operation)) {
      return {
        kind: "alert",
        message: locale === "en"
          ? `${identity} appears deleted, but before-and-after verification disagrees. Refresh Application Tracker; the page will not report a contradictory result as success.`
          : `${identity} 显示已删除，但前后核对结果不一致；请刷新求职进展确认；页面不会将矛盾结果显示为成功。`,
      };
    }
    return { kind: "status", message: completedMessage(operation, locale) };
  }
  if (operation.state === "rejected") {
    return {
      kind: "status",
      message: locale === "en"
        ? `${identity} was kept; no data was deleted or detached.`
        : `${identity} 已保留，没有删除或解绑任何数据。`,
    };
  }
  return {
    kind: "alert",
    message: locale === "en"
      ? `${identity} or linked data changed, so the old preview was not applied. Start it again in chat.`
      : `${identity} 或其关联数据已变化，旧预览未执行；请在对话中重新发起。`,
  };
}

const APPLICATION_DELETE_API = {
  listPending: getPendingApplicationDeleteOperations,
  get: getApplicationDeleteOperation,
  approve: approveApplicationDeleteOperation,
  reject: rejectApplicationDeleteOperation,
};

function applicationDeleteMessages(locale: UiLocale): BinaryTrustedOperationMessages<ApplicationDeleteOperation> {
  const en = locale === "en";
  return {
    listError: () => en
      ? "Could not refresh pending role deletions. Try again later."
      : "待确认岗位删除刷新失败，请稍后重试。",
    terminalNotice: (operation) => terminalNotice(operation, locale),
    oppositeCommandNotice: (operation, attempted) => {
      const identity = operationIdentity(operation, locale);
      if (operation.state === "completed" && attempted === "reject") {
        if (!completedResultMatchesPreview(operation)) {
          return {
            kind: "alert",
            message: en
              ? `Another window shows ${identity} as deleted, but verification disagrees. This keep request was not applied; refresh Application Tracker.`
              : `另一窗口显示已删除 ${identity}，但前后核对结果不一致；本次“保留”没有执行，请刷新求职进展确认。`,
          };
        }
        return {
          kind: "alert",
          message: en
            ? `Another window confirmed deletion; this keep request was not applied. ${completedMessage(operation, locale)}`
            : `另一窗口已确认删除；本次“保留”没有执行。${completedMessage(operation)}`,
        };
      }
      return {
        kind: "alert",
        message: en
          ? `Another window chose to keep ${identity}; this deletion was not applied and the role remains.`
          : `另一窗口已选择保留 ${identity}；本次删除没有执行，岗位仍在。`,
      };
    },
    pendingAfterResponse: (command) => command === "approve"
      ? en ? "Deletion is still processing. You can check the same choice again later." : "删除仍在处理中，可以稍后继续核对同一次选择。"
      : en ? "The keep request is still processing. You can check the same choice again later." : "保留请求仍在处理中，可以稍后继续核对同一次选择。",
    pendingAfterRecovery: (command) => command === "approve"
      ? en ? "The deletion did not complete and the role remains. Retry the same operation." : "删除请求没有完成，岗位尚未删除；可用同一操作重试。"
      : en ? "The keep request did not complete. Retry the same operation." : "保留请求没有完成；可用同一操作重试。",
    mismatchedOperation: () => en
      ? "The returned operation does not match this request. Check it again."
      : "返回的操作记录与当前请求不一致，请重新核对。",
    missingFromPending: () => en
      ? "This operation left the pending list, but its final state cannot be verified. The card remains to prevent a false result; check it again."
      : "该操作已不在待办列表，但最终状态暂时无法核对。为避免误判，卡片已保留；请重新核对。",
    unknownAfterSubmit: () => en
      ? "The network was interrupted, so deletion cannot be confirmed. The proposal is preserved; rechecking verifies the same choice."
      : "网络中断，暂时无法确认是否删除。方案会保留；重新核对只会继续确认同一次选择。",
    unknownAfterRecheck: () => en
      ? "The final state still cannot be verified. The card remains so you can check again later."
      : "最终状态仍无法核对。卡片继续保留；稍后可再次核对。",
  };
}

function ApplicationDeleteOperationCard({
  operation,
  action,
  actionsDisabled,
  uncertain,
  error,
  onApprove,
  onReject,
  onRecheck,
}: {
  operation: ApplicationDeleteOperation;
  action: ApplicationDeleteAction | null;
  actionsDisabled: boolean;
  uncertain: boolean;
  error: string | null;
  onApprove: () => void;
  onReject: () => void;
  onRecheck: () => void;
}) {
  const l = useLocalizer();
  const { locale } = useLocale();
  const { target, effect } = operation;
  const stageLabels = locale === "en" ? STAGE_LABELS_EN : STAGE_LABELS_ZH;
  const stage = stageLabels[target.stage] ?? target.stage;
  const priority = target.priority === "high"
    ? l("高", "High")
    : target.priority === "medium"
      ? l("中", "Medium")
      : target.priority === "low"
        ? l("低", "Low")
        : l("未设置", "Not set");
  const nextAction = target.next_action === null
    ? l("未设置", "Not set")
    : [target.next_action.step, target.next_action.date, target.next_action.time]
        .filter(Boolean)
        .join(" · ");
  const removedItems = [
    l("岗位记录", "role record"),
    l(`${effect.timeline_entries.length} 条历程`, `${effect.timeline_entries.length} history entries`),
    target.jd_preview || target.skills.length > 0 || target.highlights.length > 0
      ? l("已保存的岗位描述", "saved job description")
      : null,
    target.prep_artifact_present ? l("岗位准备产物", "role-preparation artifacts") : null,
  ].filter(Boolean).join(l("、", ", "));
  const retainedItems = l(
    `${effect.questions_detached.length} 道题、${effect.question_occurrences_detached} 条复盘题目出处、${effect.resumes_detached.length} 份岗位关联简历会保留并解除绑定`,
    `${effect.questions_detached.length} questions, ${effect.question_occurrences_detached} review-question sources, and ${effect.resumes_detached.length} role-linked résumés will be retained and detached`,
  );

  return (
    <ProposalDecisionCard
      id={`application-delete-operation-${operation.operation_id}`}
      tone="danger"
      title={<>{l("准备删除岗位", "Prepare to delete role")} · {target.company} · {target.position}</>}
      description={l(
        "确认后删除这条岗位及其历程；题库、复盘和简历本体会保留并解除岗位绑定。",
        "Confirmation deletes this role and its history. Questions, reviews, and résumé records are retained and detached.",
      )}
      timeLabel={formatOperationTime(operation.created_time, locale) || operation.created_time}
      actions={<>
        {uncertain && (
          <button type="button" onClick={onRecheck} disabled={actionsDisabled} className="btn btn-sm bg-panel">
            {action === "recheck" ? l("正在核对…", "Checking…") : l("重新核对", "Recheck")}
          </button>
        )}
        <button type="button" onClick={onReject} disabled={actionsDisabled || uncertain} className="btn btn-sm bg-panel">
          {action === "reject" ? l("正在保留…", "Keeping…") : l("保留岗位", "Keep role")}
        </button>
        <button type="button" onClick={onApprove} disabled={actionsDisabled || uncertain} className="btn btn-sm btn-danger">
          {action === "approve" ? l("正在删除…", "Deleting…") : l("删除岗位", "Delete role")}
        </button>
      </>}
      error={error}
    >
      <dl className="grid gap-x-5 gap-y-3 sm:grid-cols-2">
        <div>
          <dt className="text-ink-3">{l("公司与岗位", "Company and role")}</dt>
          <dd className="mt-0.5 break-words font-medium text-ink">{target.company} · {target.position}</dd>
        </div>
        <div>
          <dt className="text-ink-3">{l("当前阶段与环节", "Current stage and step")}</dt>
          <dd className="mt-0.5 break-words">{stage} · {target.current_step || l("未填写", "Not set")}</dd>
        </div>
        <div>
          <dt className="text-ink-3">{l("部门与渠道", "Department and channel")}</dt>
          <dd className="mt-0.5 break-words">{target.department || l("未填写", "Not set")} · {target.channel || l("未填写", "Not set")}</dd>
        </div>
        <div>
          <dt className="text-ink-3">{l("投递日期与优先级", "Applied date and priority")}</dt>
          <dd className="mt-0.5">{target.applied_date || l("未填写", "Not set")} · {priority}</dd>
        </div>
        <div className="sm:col-span-2">
          <dt className="text-ink-3">{l("下一步", "Next action")}</dt>
          <dd className="mt-0.5 break-words">{nextAction}</dd>
        </div>
        <div className="sm:col-span-2">
          <dt className="text-ink-3">{l("删除范围", "Deletion scope")}</dt>
          <dd className="mt-0.5 break-words">{removedItems}</dd>
        </div>
        <div className="sm:col-span-2">
          <dt className="text-ink-3">{l("保留内容", "Retained content")}</dt>
          <dd className="mt-0.5 break-words">{retainedItems}</dd>
        </div>
      </dl>
    </ProposalDecisionCard>
  );
}

export function ApplicationDeleteOperationsPanel({
  active,
  refreshSignal,
  className = "",
  operationIds,
  anchorIdForOperation,
  onOperationAppeared,
  onOperationSettled,
}: {
  active: boolean;
  refreshSignal: number;
  className?: string;
  operationIds?: readonly string[];
  anchorIdForOperation?: OperationAnchorIdResolver;
  onOperationAppeared?: (operationId: string) => void;
  onOperationSettled?: (operationId: string) => void;
}) {
  const l = useLocalizer();
  const { locale } = useLocale();
  const messages = useMemo(() => applicationDeleteMessages(locale), [locale]);

  return (
    <BinaryProposalPanelShell<ApplicationDeleteOperation>
      active={active}
      refreshSignal={refreshSignal}
      className={className}
      operationIds={operationIds}
      anchorIdForOperation={anchorIdForOperation}
      api={APPLICATION_DELETE_API}
      messages={messages}
      regionLabel={l("待确认岗位删除", "Role deletions awaiting confirmation")}
      loadingLabel={l("正在核对待确认岗位删除…", "Checking pending role deletions…")}
      noticeKeyPrefix="application-delete-notice-"
      cardKeyPrefix="application-delete-proposal-"
      pendingAnnouncement={(count) => l(`有 ${count} 条岗位删除等待确认`, `${count} role deletions await confirmation`)}
      onOperationSettled={(operation) => onOperationSettled?.(operation.operation_id)}
      onOperationAppeared={onOperationAppeared}
      renderBatchControls={({ count, command, actionsDisabled,
                              onApproveAll, onRejectAll }) => (
        <section className="rounded-2xl border border-warn/30 bg-warn-soft px-4 py-3 shadow-card">
          <p className="text-sm font-medium text-ink-1">{l(`本轮共 ${count} 条岗位等待确认`, `${count} roles await confirmation in this batch`)}</p>
          <p className="mt-1 text-xs leading-5 text-warn">
            {l("请核对下方岗位后统一处理；每条操作仍会独立校验并保留记录。", "Review the roles below, then process them together. Each operation is still validated and recorded independently.")}
          </p>
          <div className="mt-3 flex flex-wrap justify-end gap-2">
            <button type="button" className="btn btn-sm bg-panel" disabled={actionsDisabled} onClick={onRejectAll}>
              {command === "reject" ? l("正在全部保留…", "Keeping all…") : l("全部保留", "Keep all")}
            </button>
            <button type="button" className="btn btn-sm btn-danger" disabled={actionsDisabled} onClick={onApproveAll}>
              {command === "approve" ? l("正在删除…", "Deleting…") : l(`全部删除（${count}）`, `Delete all (${count})`)}
            </button>
          </div>
        </section>
      )}
      renderCard={({ operation, action, actionsDisabled, uncertain, error,
                     onApprove, onReject, onRecheck }) => (
        <ApplicationDeleteOperationCard
          key={operation.operation_id}
          operation={operation}
          action={action}
          actionsDisabled={actionsDisabled}
          uncertain={uncertain}
          error={error}
          onApprove={onApprove}
          onReject={onReject}
          onRecheck={onRecheck}
        />
      )}
    />
  );
}
