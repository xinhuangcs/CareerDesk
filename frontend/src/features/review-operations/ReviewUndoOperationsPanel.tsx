import {
  approveReviewUndoOperation,
  getPendingReviewUndoOperations,
  getReviewUndoOperation,
  rejectReviewUndoOperation,
} from "./reviewUndoOperationApi";
import { useMemo } from "react";
import { formatDate } from "../../i18n/formatters";
import type { UiLocale } from "../../i18n/i18n";
import { useLocale } from "../../i18n/localePreference";
import { useLocalizer } from "../../i18n/useLocalizer";
import type { ReviewUndoOperation } from "./reviewUndoOperationContract";
import {
  type BinaryTrustedOperationAction,
  type BinaryTrustedOperationMessages,
  type BinaryTrustedOperationNoticePayload,
} from "../operations/useBinaryTrustedOperationQueue";
import { BinaryProposalPanelShell } from "../operations/BinaryProposalPanelShell";
import { type OperationAnchorIdResolver } from "../operations/operationAnchorPortal";

type ReviewUndoAction = BinaryTrustedOperationAction;

const STAGE_LABELS: Record<string, string> = {
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
  backlog: "Considering", applied: "Applied", written_test: "Assessment",
  interviewing: "Interviewing", offer: "Offer", withdrawn: "Withdrawn",
  rejected: "Rejected", pooled: "On hold",
};

const OUTCOME_LABELS: Record<string, string> = {
  passed: "通过",
  failed: "未通过",
  cancelled: "取消",
};
const OUTCOME_LABELS_EN: Record<string, string> = {
  passed: "Passed", failed: "Not passed", cancelled: "Cancelled",
};

function formatOperationTime(value: string, locale: UiLocale): string {
  return formatDate(value, locale, "dateTime");
}

function operationIdentity(operation: ReviewUndoOperation, locale: UiLocale): string {
  const target = operation.target;
  const time = formatOperationTime(target.review_created_time, locale) || target.review_created_time;
  return `${target.company} · ${target.position} · ${time}`;
}

function completedMessage(operation: ReviewUndoOperation, locale: UiLocale): string {
  const removed = operation.result?.removed;
  if (removed) {
    const timelineEntries = removed.timeline_entries;
    const application = operation.effect.application;
    const stageChange = application.record_exists && !application.record_retained
      ? (locale === "en" ? ", and removed the role created by this review" : "，并移除这次复盘新建的岗位")
      : application.expected?.stage !== application.replacement?.stage
      ? `${locale === "en" ? ", and recalculated the stage as " : "，岗位阶段已重算为 "}${application.replacement
        ? ((locale === "en" ? STAGE_LABELS_EN : STAGE_LABELS)[application.replacement.stage] ?? application.replacement.stage)
        : (locale === "en" ? "Considering" : "待定")}`
      : "";
    return locale === "en" ? `${operationIdentity(operation, locale)} was undone and ${timelineEntries} linked history entries were removed${stageChange}.` : `${operationIdentity(operation, locale)} 已撤销，并清除 ${timelineEntries} 条关联历程${stageChange}。`;
  }
  return locale === "en" ? `${operationIdentity(operation, locale)} was undone.` : `${operationIdentity(operation, locale)} 已撤销。`;
}

function terminalNotice(operation: ReviewUndoOperation, locale: UiLocale): BinaryTrustedOperationNoticePayload {
  const identity = operationIdentity(operation, locale);
  if (operation.state === "completed") {
    return { kind: "status", message: completedMessage(operation, locale) };
  }
  if (operation.state === "rejected") {
    return {
      kind: "status",
      message: locale === "en" ? `${identity} was kept; no data was deleted.` : `${identity} 已保留，没有删除任何数据。`,
    };
  }
  return {
    kind: "alert",
    message: locale === "en" ? `The review or linked data for ${identity} changed. The old preview was not executed; start again in chat.` : `${identity} 的复盘或关联数据已变化，旧预览未执行；请在对话中重新发起。`,
  };
}

const REVIEW_UNDO_API = {
  listPending: getPendingReviewUndoOperations,
  get: getReviewUndoOperation,
  approve: approveReviewUndoOperation,
  reject: rejectReviewUndoOperation,
};

function reviewUndoMessages(locale: UiLocale): BinaryTrustedOperationMessages<ReviewUndoOperation> { return {
  listError: () => locale === "en" ? "Could not refresh pending review undos. Try again later." : "待确认复盘撤销刷新失败，请稍后重试。",
  terminalNotice: (operation) => terminalNotice(operation, locale),
  oppositeCommandNotice: (operation, attempted) => {
    const identity = operationIdentity(operation, locale);
    if (operation.state === "completed" && attempted === "reject") {
      return {
        kind: "alert",
        message: locale === "en" ? `Another window already confirmed undo; this keep command did not run. ${completedMessage(operation, locale)}` : `另一窗口已确认撤销；本次“保留”没有执行。${completedMessage(operation, locale)}`,
      };
    }
    return {
      kind: "alert",
      message: locale === "en" ? `Another window kept ${identity}; this undo did not run and the review remains.` : `另一窗口已选择保留 ${identity}；本次撤销没有执行，复盘仍在。`,
    };
  },
  pendingAfterResponse: (command) => command === "approve"
    ? (locale === "en" ? "Undo is still processing; you can check the same choice later." : "撤销仍在处理中，可以稍后继续核对同一次选择。")
    : (locale === "en" ? "Keep is still processing; you can check the same choice later." : "保留请求仍在处理中，可以稍后继续核对同一次选择。"),
  pendingAfterRecovery: (command) => command === "approve"
    ? (locale === "en" ? "Undo did not finish and the review has not been deleted; retry the same action." : "撤销请求没有完成，复盘尚未删除；可用同一操作重试。")
    : (locale === "en" ? "Keep did not finish; retry the same action." : "保留请求没有完成；可用同一操作重试。"),
  mismatchedOperation: () => locale === "en"
    ? "The returned operation does not match this request. Check it again."
    : "返回的操作记录与当前请求不一致，请重新核对。",
  missingFromPending: () => locale === "en"
    ? "This operation left the pending list, but its final state cannot be verified. The card remains to prevent a false result; check it again."
    : "该操作已不在待办列表，但最终状态暂时无法核对。为避免误判，卡片已保留；请重新核对。",
  unknownAfterSubmit: (_submitReason, _readReason) => {
    return locale === "en" ? "The connection was interrupted and the undo result is unknown. The proposal remains; rechecking only continues the same choice." : "网络中断，暂时无法确认是否撤销。方案会保留；重新核对只会继续确认同一次选择。";
  },
  unknownAfterRecheck: () => {
    return locale === "en" ? "The final state still cannot be verified. The card remains so you can check again later." : "最终状态仍无法核对。卡片继续保留；稍后可再次核对。";
  },
}; }

function ReviewUndoOperationCard({
  operation,
  action,
  actionsDisabled,
  uncertain,
  error,
  onApprove,
  onReject,
  onRecheck,
}: {
  operation: ReviewUndoOperation;
  action: ReviewUndoAction | null;
  actionsDisabled: boolean;
  uncertain: boolean;
  error: string | null;
  onApprove: () => void;
  onReject: () => void;
  onRecheck: () => void;
}) {
  const { locale } = useLocale();
  const t = useLocalizer();
  const stages = locale === "en" ? STAGE_LABELS_EN : STAGE_LABELS;
  const outcomes = locale === "en" ? OUTCOME_LABELS_EN : OUTCOME_LABELS;
  const { target, effect } = operation;
  const createdLabel = formatOperationTime(operation.created_time, locale);
  const application = effect.application;
  const expected = application.expected;
  const replacement = application.replacement;
  const stageChanged = expected?.stage !== replacement?.stage;
  const stepChanged = expected?.current_step !== replacement?.current_step;

  return (
    <section
      aria-labelledby={`review-undo-operation-${operation.operation_id}`}
      className="card overflow-hidden border-bad/30"
    >
      <div className="border-b border-line bg-bad-soft px-4 py-3">
        <div className="flex flex-wrap items-start justify-between gap-2">
          <div className="min-w-0">
            <div className="flex items-center gap-2">
              <span className="h-2 w-2 shrink-0 rounded-full bg-bad" aria-hidden />
              <h2
                id={`review-undo-operation-${operation.operation_id}`}
                className="break-words text-sm font-semibold"
              >
                {t("准备撤销复盘", "Ready to undo review")} · {target.company} · {target.position}
              </h2>
            </div>
            <p className="mt-1 text-xs leading-relaxed text-bad">
              {t("撤销这条复盘及其历程；岗位阶段和当前环节会按剩余记录重新计算。", "Undo this review and its history; the application stage and current step will be recalculated from remaining records.")}
            </p>
          </div>
          {createdLabel && (
            <span className="shrink-0 text-xs tabular-nums text-ink-3">{createdLabel}</span>
          )}
        </div>
        <div className="mt-3 flex flex-wrap items-center justify-end gap-2">
          {uncertain && (
            <button
              type="button"
              onClick={onRecheck}
              disabled={actionsDisabled}
              className="btn btn-sm bg-panel"
            >
              {action === "recheck" ? t("正在核对…", "Checking…") : t("重新核对状态", "Check status again")}
            </button>
          )}
          <button
            type="button"
            onClick={onReject}
            disabled={actionsDisabled || uncertain}
            className="btn btn-sm bg-panel"
          >
            {action === "reject" ? t("正在保留…", "Keeping…") : t("✕ 保留复盘", "✕ Keep review")}
          </button>
          <button
            type="button"
            onClick={onApprove}
            disabled={actionsDisabled || uncertain}
            className="btn btn-sm btn-danger"
          >
            {action === "approve" ? t("正在撤销…", "Undoing…") : t("✓ 撤销复盘", "✓ Undo review")}
          </button>
        </div>
      </div>

      <div className="space-y-3 px-4 py-3">
        <div className="rounded-xl border border-line bg-panel px-3.5 py-3">
          <h3 className="text-[11px] font-medium text-ink-3">{t("撤销内容", "Content to undo")}</h3>
          <blockquote
            className="mt-1 max-h-28 overflow-y-auto whitespace-pre-wrap break-words text-sm leading-relaxed text-ink-2"
            tabIndex={0}
            aria-label={t("要撤销的复盘原文", "Original review to undo")}
          >
            {target.content_preview || t("（无可展示原文）", "(No preview available)")}
          </blockquote>
          {target.content_truncated && (
            <p className="mt-1 text-xs text-ink-3">{t("内容较长，此处显示摘要。", "This content is long, so a summary is shown.")}</p>
          )}
        </div>

        <div>
          <h3 className="text-xs font-semibold text-ink-2">
            {t(`将移除 ${effect.timeline_entries.length} 条历程`, `${effect.timeline_entries.length} history entries will be removed`)}
          </h3>
          {effect.timeline_entries.length > 0 ? (
            <ul className="mt-1.5 divide-y divide-line overflow-hidden rounded-xl border border-line bg-panel">
              {effect.timeline_entries.map((entry) => (
                <li key={entry.id} className="px-3 py-2 text-xs text-ink-2">
                  <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                    <span className="font-medium">{entry.step ?? t("阶段更新", "Stage update")}</span>
                    {entry.outcome && <span>{outcomes[entry.outcome]}</span>}
                    {entry.occurred_date && (
                      <span className="tabular-nums text-ink-3">{entry.occurred_date}</span>
                    )}
                  </div>
                  {(entry.from_stage !== entry.to_stage
                      || entry.from_step !== entry.to_step) && (
                    <p className="mt-1 text-ink-3">
                      {stages[entry.from_stage] ?? entry.from_stage}
                      {entry.from_step ? ` · ${entry.from_step}` : ""}
                      {" → "}
                      {stages[entry.to_stage] ?? entry.to_stage}
                      {entry.to_step ? ` · ${entry.to_step}` : ""}
                    </p>
                  )}
                  {entry.summary && (
                    <p className="mt-1 line-clamp-2 whitespace-pre-wrap break-words text-ink-3">
                      {entry.summary}
                    </p>
                  )}
                </li>
              ))}
            </ul>
          ) : (
            <p className="mt-1 text-xs text-ink-3">{t("没有关联历程。", "No linked history.")}</p>
          )}
        </div>

        <div className="rounded-xl bg-panel-2 px-3.5 py-3 text-xs leading-relaxed text-ink-2">
          <p className="font-semibold text-ink">{t("撤销后", "After undo")}</p>
          <ul className="mt-1.5 space-y-1">
            {application.record_exists && (
              <li>
                {application.record_retained && expected && replacement
                  ? <>{t("岗位阶段：", "Application stage: ")}{stageChanged
                    ? `${stages[expected.stage] ?? expected.stage} → ${stages[
                      replacement.stage
                    ] ?? replacement.stage}`
                    : t(`保持${stages[replacement.stage] ?? replacement.stage}`, `Keep ${stages[replacement.stage] ?? replacement.stage}`)}</>
                  : t("删除这个由本次复盘新建的岗位", "Delete the role created by this review")}
              </li>
            )}
            {application.record_retained && expected && replacement && stepChanged && (
              <li>
                {t("当前环节：", "Current step: ")}{expected.current_step ?? t("未填写", "Not set")} → {replacement.current_step ?? t("未填写", "Not set")}
              </li>
            )}
            {application.record_retained && replacement?.next_action && (
              <li>
                {t("下一步：", "Next action: ")}{stages[replacement.next_action.stage]
                  ?? replacement.next_action.stage} · {replacement.next_action.step}
                {replacement.next_action.date ? ` · ${replacement.next_action.date}` : ""}
                {replacement.next_action.time ? ` ${replacement.next_action.time}` : ""}
              </li>
            )}
            {effect.questions_archived.length > 0 && (
              <li>{t(`归档 ${effect.questions_archived.length} 道仅由这条复盘提供的题目`, `Archive ${effect.questions_archived.length} questions supplied only by this review`)}</li>
            )}
            {effect.status_logs_removed > 0 && (
              <li>{t(`移除 ${effect.status_logs_removed} 条关联状态记录`, `Remove ${effect.status_logs_removed} linked status records`)}</li>
            )}
            {!application.record_exists && <li>{t("岗位已不存在，不会重新创建。", "The role no longer exists and will not be recreated.")}</li>}
          </ul>
          {application.record_retained && (
            <p className="mt-2 border-t border-line pt-2 text-ink-3">
              {t("岗位本身会保留；上方展示的是撤销后的完整状态。", "The role itself remains; the complete post-undo state is shown above.")}
            </p>
          )}
        </div>
      </div>
      {error && (
        <p role="alert" className="border-t border-line bg-bad-soft px-4 py-2.5 text-xs text-bad">
          {error}
        </p>
      )}
    </section>
  );
}
export function ReviewUndoOperationsPanel({
  active,
  refreshSignal,
  className = "",
  operationIds,
  anchorIdForOperation,
  onOperationSettled,
  onCanonicalOperationSettled,
}: {
  active: boolean;
  refreshSignal: number;
  className?: string;
  operationIds?: readonly string[];
  anchorIdForOperation?: OperationAnchorIdResolver;
  onOperationSettled?: (operationId: string) => void;
  onCanonicalOperationSettled?: (operation: ReviewUndoOperation) => void;
}) {
  const { locale } = useLocale();
  const t = useLocalizer();
  const messages = useMemo(() => reviewUndoMessages(locale), [locale]);
  const batchAnchorIds = operationIds === undefined || anchorIdForOperation === undefined
    ? null
    : operationIds.map(anchorIdForOperation);
  const canRenderBatchControls = batchAnchorIds === null
    || (batchAnchorIds.every((anchorId) => anchorId !== null)
      && new Set(batchAnchorIds).size === 1);
  return (
    <BinaryProposalPanelShell<ReviewUndoOperation>
      active={active}
      refreshSignal={refreshSignal}
      className={className}
      operationIds={operationIds}
      anchorIdForOperation={anchorIdForOperation}
      api={REVIEW_UNDO_API}
      messages={messages}
      regionLabel={t("待确认复盘撤销", "Pending review undos")}
      loadingLabel={t("正在核对待确认复盘撤销…", "Checking pending review undos…")}
      noticeKeyPrefix="review-undo-notice-"
      cardKeyPrefix="review-undo-proposal-"
      renderBatchControls={canRenderBatchControls ? ({
        count, command, actionsDisabled, onApproveAll, onRejectAll,
      }) => (
        <div className="flex flex-wrap items-center justify-between gap-2 rounded-xl border border-bad/30 bg-bad-soft px-4 py-3">
          <p className="text-sm font-semibold text-bad">{t(`本批共 ${count} 条复盘，可统一处理`, `${count} reviews can be handled together`)}</p>
          <span className="flex flex-wrap gap-2">
            <button
              type="button"
              onClick={onRejectAll}
              disabled={actionsDisabled}
              className="btn btn-sm bg-panel"
            >
              {command === "reject" ? t("正在全部保留…", "Keeping all…") : t("全部保留", "Keep all")}
            </button>
            <button
              type="button"
              onClick={onApproveAll}
              disabled={actionsDisabled}
              className="btn btn-sm btn-danger"
            >
              {command === "approve" ? t("正在撤销整批…", "Undoing batch…") : t(`撤销整批（${count}）`, `Undo batch (${count})`)}
            </button>
          </span>
        </div>
      ) : undefined}
      onOperationSettled={(operation) => {
        onOperationSettled?.(operation.operation_id);
        onCanonicalOperationSettled?.(operation);
      }}
      renderCard={({ operation, action, actionsDisabled, uncertain, error,
                     onApprove, onReject, onRecheck }) => (
        <ReviewUndoOperationCard
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
