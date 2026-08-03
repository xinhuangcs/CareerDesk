import type {
  ReviewRecordApplicationStage,
  ReviewRecordNextAction,
  ReviewRecordOperation,
} from "./reviewRecordOperationContract";
import { formatDate } from "../../i18n/formatters";
import type { UiLocale } from "../../i18n/i18n";
import { useLocale } from "../../i18n/localePreference";
import { useLocalizer } from "../../i18n/useLocalizer";
import {
  REVIEW_RECORD_TARGET_STATE_LABELS,
  REVIEW_RECORD_UNDO_BLOCK_LABELS,
  reviewRecordIntegrityIssue,
} from "./reviewRecordOperationContract";

const STAGE_LABELS: Record<ReviewRecordApplicationStage, string> = {
  backlog: "待定", applied: "已投递", written_test: "笔试中", interviewing: "面试中",
  offer: "Offer", pooled: "泡池子", withdrawn: "不再跟进", rejected: "已挂",
};
const STAGE_LABELS_EN: Record<ReviewRecordApplicationStage, string> = {
  backlog: "Considering", applied: "Applied", written_test: "Assessment", interviewing: "Interviewing",
  offer: "Offer", pooled: "On hold", withdrawn: "Withdrawn", rejected: "Rejected",
};

const OUTCOME_LABELS = { passed: "通过", failed: "未通过", cancelled: "取消" } as const;
const OUTCOME_LABELS_EN = { passed: "Passed", failed: "Not passed", cancelled: "Cancelled" } as const;

function assertNever(value: never): never {
  throw new Error(`Unhandled review-record state: ${String(value)}`);
}

function formatTime(value: string, locale: UiLocale): string {
  return formatDate(value, locale, "dateTime") || value;
}

function formatNextAction(action: ReviewRecordNextAction | null, locale: UiLocale): string {
  if (!action) return locale === "en" ? "Not set" : "未设置";
  const stages = locale === "en" ? STAGE_LABELS_EN : STAGE_LABELS;
  return [
    action.step,
    locale === "en" ? `then ${stages[action.stage]}` : `完成后进入${stages[action.stage]}`,
    action.date,
    action.time,
  ].filter(Boolean).join(" · ");
}

export function ReviewRecordOperationCard({
  operation,
  actionsDisabled,
  readUncertain,
  clarificationPending,
  proposalAction,
  proposalRecoveryAction,
  preparing,
  error,
  notice,
  onPrepareUndo,
  onApprove,
  onReject,
  onContinue,
  pendingActions = "visible",
  appearance = "card",
}: {
  operation: ReviewRecordOperation;
  actionsDisabled: boolean;
  readUncertain: boolean;
  clarificationPending: boolean;
  proposalAction: "approve" | "reject" | null;
  proposalRecoveryAction: "approve" | "reject" | null;
  preparing: boolean;
  error: string | null;
  notice: string | null;
  onPrepareUndo: () => void;
  onApprove: () => void;
  onReject: () => void;
  onContinue: () => void;
  pendingActions?: "visible" | "hidden";
  appearance?: "card" | "batch-item";
}) {
  const { locale } = useLocale();
  const t = useLocalizer();
  const stages = locale === "en" ? STAGE_LABELS_EN : STAGE_LABELS;
  const outcomes = locale === "en" ? OUTCOME_LABELS_EN : OUTCOME_LABELS;
  const integrityIssue = reviewRecordIntegrityIssue(operation);
  const corrupt = integrityIssue !== null;
  const result = operation.state === "completed" ? operation.result : null;
  const pendingConfirmation = operation.state === "pending_confirmation" && !corrupt;
  const preview = pendingConfirmation ? operation.preview : null;
  const targetPlan = preview?.target_plan ?? null;
  const extraction = result?.extraction ?? preview?.extraction ?? null;
  const identityMissing = pendingConfirmation && (preview?.missing.some(
    (item) => item.field === "company" || item.field === "position",
  ) ?? false);
  const applied = result?.outcome === "applied" && !corrupt;
  const needsClarification = result?.outcome === "needs_clarification" && !corrupt;

  let title: string;
  let description: string;
  let toneClass: string;
  let dotClass: string;
  switch (operation.state) {
    case "processing":
      title = t("求职进展已保存，正在整理", "Progress saved and being organized");
      description = t("原话已经安全保留；当前尚未改变岗位状态。", "Your original wording is safe; the application has not changed yet.");
      toneClass = "border-warn/30 bg-warn-soft";
      dotClass = "bg-warn";
      break;
    case "pending_confirmation":
      title = `${t("准备写入进展", "Ready to record progress")} · ${targetPlan?.company ?? extraction?.company ?? t("公司未提供", "Company not provided")} · ${targetPlan?.position ?? extraction?.position ?? t("岗位未提供", "Role not provided")}`;
      description = identityMissing
        ? t("暂时无法安全定位岗位；确认只会保留草稿。", "The role cannot be identified safely yet; confirming will only retain a draft.")
        : t("请分别核对已发生事实、确认后的阶段与环节和下一步安排；确认后才会一次性写入。", "Review what happened, the resulting stage and step, and the next action. Nothing is written until you confirm.");
      toneClass = "border-warn/30 bg-warn-soft";
      dotClass = "bg-warn";
      break;
    case "completed":
      if (applied && result?.application) {
        title = `${t("进展已记录", "Progress recorded")} · ${result.application.company} · ${result.application.position}`;
        description = operation.undo_block_reason === "target_changed"
          ? t("岗位后来已有更新；下方仅是本次发布时的历史结果。", "This application changed later; the result below is the historical state at publication.")
          : operation.undo_block_reason === "target_not_applied"
            ? t("这份复盘当前已撤销或不再发布。", "This review has been undone or is no longer published.")
            : t("已同步岗位阶段、当前环节、下一步、进展记录和题库。", "Stage, current step, next action, history, and question bank are now synchronized.");
        toneClass = operation.undo_available ? "border-ok/30 bg-ok-soft" : "border-info/30 bg-info-soft";
        dotClass = operation.undo_available ? "bg-ok" : "bg-info";
      } else if (needsClarification) {
        title = t("进展原话已保存 · 可选补充", "Original progress saved · Optional details");
        description = t("补齐岗位身份后即可重新核对，不会阻止其它独立进展。", "Add the role identity to review this again; other independent updates remain available.");
        toneClass = "border-warn/30 bg-warn-soft";
        dotClass = "bg-warn";
      } else {
        title = t("进展结果需要核对", "Progress result needs review");
        description = corrupt
          ? t("操作结果未通过完整性核对。", "The operation result failed integrity checks.")
          : t("结果类型无法识别", "The result type is not recognized");
        toneClass = "border-bad/30 bg-bad-soft";
        dotClass = "bg-bad";
      }
      break;
    case "failed":
      title = t("进展未写入岗位或题库", "Progress was not written to the application or question bank");
      description = t("整理没有完成，原话仍会保留。", "Processing did not finish, but your original wording remains saved.");
      toneClass = "border-bad/30 bg-bad-soft";
      dotClass = "bg-bad";
      break;
    case "superseded":
      title = t("这次处理已被后续补充取代", "This result was replaced by a later update");
      description = t("请以后续补充结果为准。", "Use the later update as the current result.");
      toneClass = "border-info/30 bg-info-soft";
      dotClass = "bg-info";
      break;
    case "rejected":
      title = t("已放弃这份进展方案", "Progress proposal discarded");
      description = t("没有改动岗位、求职进展或题库。", "No application, history, or question-bank data was changed.");
      toneClass = "border-line bg-panel-2";
      dotClass = "bg-ink-3";
      break;
    default:
      return assertNever(operation.state);
  }

  return (
    <section aria-labelledby={`review-record-operation-title-${operation.operation_id}`} className={appearance === "card" ? "card overflow-hidden" : "mt-2"}>
      <div className={`${appearance === "batch-item" ? "sr-only" : "flex"} flex-wrap items-start justify-between gap-2 border-b px-4 py-3 ${toneClass}`}>
        <div className="min-w-0">
          <div className="flex items-center gap-2"><span className={`h-2 w-2 shrink-0 rounded-full ${dotClass}`} /><h2 id={`review-record-operation-title-${operation.operation_id}`} className="break-words text-sm font-semibold">{title}</h2></div>
          <p className="mt-1 text-xs leading-relaxed text-ink-2">{description}</p>
        </div>
        <time dateTime={operation.created_time} className="shrink-0 text-xs text-ink-3">{formatTime(operation.created_time, locale)}</time>
      </div>

      <div className={appearance === "card"
        ? "space-y-3 border-b border-line bg-panel px-4 py-3 text-xs"
        : "space-y-2 text-xs"}
      >
        {!pendingConfirmation && (
          <dl className="grid gap-x-4 gap-y-2 sm:grid-cols-2">
            <div><dt className="text-ink-3">{t("记录方式", "Entry type")}</dt><dd className="mt-0.5 text-ink-2">{operation.mode === "initial" ? t("初次记录", "Initial entry") : t("继续补充", "Follow-up")}</dd></div>
            <div><dt className="text-ink-3">{t("记录状态", "Entry status")}</dt><dd className="mt-0.5 text-ink-2">{locale === "en" ? ({ recorded: "Recorded", draft: "Draft", unknown: "Unknown" } as Record<string, string>)[operation.target_current_state] ?? operation.target_current_state : REVIEW_RECORD_TARGET_STATE_LABELS[operation.target_current_state]}</dd></div>
          </dl>
        )}

        {targetPlan && appearance === "card" && (
          <section aria-label={t("关联岗位", "Linked role")} className="rounded-lg bg-panel-2 px-3 py-2.5">
            <p className="text-[11px] text-ink-3">{targetPlan.kind === "existing" ? t("关联现有岗位", "Existing role") : t("新建岗位", "New role")}</p>
            <p className="mt-0.5 font-semibold text-ink">{targetPlan.company} · {targetPlan.position}</p>
          </section>
        )}

        {extraction?.history && (
          <section aria-label={t("本次发生", "What happened")} className="rounded-lg border border-line px-3 py-2.5">
            <p className="font-semibold text-ink">{t("本次发生", "What happened")}</p>
            <p className="mt-1 text-ink-2">{extraction.history.step ?? extraction.history.summary ?? t("已记录进展", "Progress recorded")}{extraction.history.date ? ` · ${extraction.history.date}` : ""}{extraction.history.outcome ? ` · ${outcomes[extraction.history.outcome]}` : ""}</p>
            {extraction.history.summary && extraction.history.summary !== extraction.history.step && <p className="mt-1 whitespace-pre-wrap text-ink-3">{extraction.history.summary}</p>}
          </section>
        )}

        {(extraction?.projected_state || targetPlan) && (
          <section aria-label={t("确认后的阶段与环节", "Resulting stage and step")} className="rounded-lg border border-line px-3 py-2.5">
            <p className="font-semibold text-ink">{t("确认后的阶段与环节", "Resulting stage and step")}</p>
            {targetPlan ? (
              <p className="mt-1 text-ink-2">
                {stages[targetPlan.current_stage]}{targetPlan.current_step ? ` · ${targetPlan.current_step}` : ""}
                {" → "}
                {stages[targetPlan.projected_stage]}{targetPlan.projected_step ? ` · ${targetPlan.projected_step}` : ""}
              </p>
            ) : extraction?.projected_state ? (
              <p className="mt-1 text-ink-2">{extraction.projected_state.stage ? stages[extraction.projected_state.stage] : t("阶段不变", "Stage unchanged")}{extraction.projected_state.current_step ? ` · ${extraction.projected_state.current_step}` : ""}</p>
            ) : null}
          </section>
        )}

        {(extraction?.clear_next_action || extraction?.next_action || targetPlan?.current_next_action || targetPlan?.projected_next_action) && (
          <section aria-label={t("下一步安排", "Next action")} className="rounded-lg border border-line px-3 py-2.5">
            <p className="font-semibold text-ink">{t("下一步安排", "Next action")}</p>
            {targetPlan && formatNextAction(targetPlan.current_next_action, locale) !== formatNextAction(targetPlan.projected_next_action, locale) ? (
              <p className="mt-1 text-ink-2">{formatNextAction(targetPlan.current_next_action, locale)} → {formatNextAction(targetPlan.projected_next_action, locale)}</p>
            ) : (
              <p className="mt-1 text-ink-2">{extraction?.clear_next_action
                ? t("已清空现有安排", "Existing action cleared")
                : formatNextAction(extraction?.next_action ?? targetPlan?.projected_next_action ?? null, locale)}</p>
            )}
            {(extraction?.next_action?.note ?? targetPlan?.projected_next_action?.note) && <p className="mt-1 whitespace-pre-wrap text-ink-3">{extraction?.next_action?.note ?? targetPlan?.projected_next_action?.note}</p>}
          </section>
        )}

        {((needsClarification && result) || (pendingConfirmation && (preview?.missing.length ?? 0) > 0)) && (
          <div className="rounded-lg bg-warn-soft px-3 py-2 text-warn"><p className="font-medium">{t("可选补充信息", "Optional details")}</p><ul className="mt-1 list-disc space-y-1 pl-4">{(result?.missing ?? preview?.missing ?? []).map((item, index) => <li key={`${item.field}:${index}`}>{item.ask}</li>)}</ul></div>
        )}
        {(operation.state === "failed" || operation.state === "superseded") && operation.error && <p className="rounded-lg bg-panel-2 px-3 py-2 text-ink-2">{t("处理未完成，请稍后重试或重新发起。", "Processing did not finish. Try again later or start a new review.")}</p>}
        {applied && result?.derivation && <p className="text-ink-3">{t(`历程 ${result.derivation.timeline_entry_ids.length} 条 · 题目 ${result.derivation.question_ids.length} 条 · 知识点 ${result.derivation.knowledge_point_ids.length} 条`, `${result.derivation.timeline_entry_ids.length} history entries · ${result.derivation.question_ids.length} questions · ${result.derivation.knowledge_point_ids.length} knowledge points`)}</p>}
      </div>

      {((pendingConfirmation && pendingActions === "visible") || applied || needsClarification || readUncertain || corrupt) && (
        <div className="flex flex-wrap items-center justify-between gap-2 border-t border-line bg-panel-2/40 px-4 py-3">
          <p className="min-w-0 flex-1 text-xs text-ink-3">
            {readUncertain ? t("最近一次核对失败或不完整；写入入口暂时关闭。", "The latest check failed or was incomplete; writing is temporarily disabled.") : corrupt ? t("结果异常，写入入口已关闭。", "The result is invalid; writing is disabled.") : pendingConfirmation ? proposalRecoveryAction ? t("上次动作结果仍在核对；只能继续同一动作。", "The previous action is still being checked; only that same action can continue.") : identityMissing ? t("确认只保留草稿；补充岗位身份后再写入。", "Confirming retains a draft only; add the role identity before writing.") : t("确认后一次性写入事实、阶段与环节和下一步。", "Confirm to write the facts, resulting stage and step, and next action together.") : needsClarification ? clarificationPending ? t("可以补充岗位信息后重新核对。", "Add role details to review again.") : t("当前补充入口已关闭。", "Additional details are currently unavailable.") : !operation.undo_available ? operation.undo_block_reason ? (locale === "en" ? ({ target_changed: "The application changed later and can no longer be undone from this preview.", target_not_applied: "This review is no longer applied." } as Record<string, string>)[operation.undo_block_reason] ?? t("当前未开放撤销预览。", "Undo preview is unavailable.") : REVIEW_RECORD_UNDO_BLOCK_LABELS[operation.undo_block_reason]) : t("当前未开放撤销预览。", "Undo preview is unavailable.") : notice ? t("撤销预览已经生成。", "Undo preview is ready.") : t("生成撤销预览不会立即删除，仍需确认。", "Generating an undo preview deletes nothing; confirmation is still required.")}
          </p>
          {pendingConfirmation && pendingActions === "visible" && <span className="flex shrink-0 flex-wrap gap-2"><button type="button" onClick={onContinue} disabled={actionsDisabled || proposalRecoveryAction !== null} className="btn btn-sm">{t("补充信息（可选）", "Add details (optional)")}</button><button type="button" onClick={onReject} disabled={actionsDisabled || proposalRecoveryAction === "approve"} className="btn btn-sm">{proposalAction === "reject" ? t("正在放弃…", "Discarding…") : t("✕ 放弃", "✕ Discard")}</button><button type="button" onClick={onApprove} disabled={actionsDisabled || proposalRecoveryAction === "reject"} className="btn-primary btn-sm">{proposalAction === "approve" ? t("正在写入…", "Writing…") : identityMissing ? t("✓ 保留草稿", "✓ Keep draft") : t("✓ 写入", "✓ Write")}</button></span>}
          {applied && operation.undo_available && <button type="button" onClick={onPrepareUndo} disabled={actionsDisabled} className="btn btn-sm shrink-0">{preparing ? t("正在生成预览…", "Generating preview…") : notice ? t("重新核对撤销预览", "Refresh undo preview") : t("生成撤销预览", "Generate undo preview")}</button>}
          {needsClarification && clarificationPending && <button type="button" onClick={onContinue} disabled={actionsDisabled} className="btn btn-sm shrink-0">{t("补充信息（可选）", "Add details (optional)")}</button>}
        </div>
      )}
      {notice && <p role="status" className="border-t border-line bg-ok-soft px-4 py-2.5 text-xs text-ok">{notice}</p>}
      {error && <p role="alert" className="border-t border-line bg-bad-soft px-4 py-2.5 text-xs text-bad">{error}</p>}
    </section>
  );
}
