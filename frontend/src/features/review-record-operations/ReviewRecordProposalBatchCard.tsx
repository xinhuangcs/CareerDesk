import { useEffect, useMemo, useState } from "react";
import { formatDate } from "../../i18n/formatters";
import { useLocale } from "../../i18n/localePreference";
import { useLocalizer, type Localize } from "../../i18n/useLocalizer";

import { ProposalDecisionCard } from "../operations/ProposalDecisionCard";
import type {
  ReviewRecordApplicationStage,
  ReviewRecordExtraction,
  ReviewRecordOperation,
} from "./reviewRecordOperationContract";
import {
  buildReviewRecordProposalBatchDecisions,
  countReviewRecordProposalBatch,
  type ReviewRecordProposalBatchDecision,
} from "./reviewRecordProposalBatch";

const STAGES: readonly (readonly [ReviewRecordApplicationStage, string])[] = [
  ["backlog", "待定"], ["applied", "已投递"], ["written_test", "笔试中"],
  ["interviewing", "面试中"], ["offer", "Offer"], ["pooled", "泡池子"],
  ["withdrawn", "不再跟进"], ["rejected", "已挂"],
];
const STAGES_EN: readonly (readonly [ReviewRecordApplicationStage, string])[] = [
  ["backlog", "Considering"], ["applied", "Applied"], ["written_test", "Assessment"],
  ["interviewing", "Interviewing"], ["offer", "Offer"], ["pooled", "On hold"],
  ["withdrawn", "Withdrawn"], ["rejected", "Rejected"],
];

const stageLabel = (stage: ReviewRecordApplicationStage | null, english = false) => (
  stage === null ? (english ? "Stage unchanged" : "阶段不变") : (english ? STAGES_EN : STAGES).find(([value]) => value === stage)?.[1] ?? stage
);

function normalizedText(value: string | null): string | null {
  const trimmed = value?.trim() ?? "";
  return trimmed || null;
}

function normalizeEditedExtraction(extraction: ReviewRecordExtraction): ReviewRecordExtraction {
  return {
    ...extraction,
    company: normalizedText(extraction.company),
    position: normalizedText(extraction.position),
    channel: normalizedText(extraction.channel),
    history: extraction.history === null ? null : {
      ...extraction.history,
      step: normalizedText(extraction.history.step),
      summary: normalizedText(extraction.history.summary),
    },
    projected_state: extraction.projected_state === null ? null : {
      ...extraction.projected_state,
      current_step: normalizedText(extraction.projected_state.current_step),
    },
    next_action: extraction.next_action === null ? null : {
      ...extraction.next_action,
      step: extraction.next_action.step.trim(),
      note: normalizedText(extraction.next_action.note),
    },
  };
}

function sameNextAction(
  left: ReviewRecordExtraction["next_action"],
  right: ReviewRecordExtraction["next_action"],
): boolean {
  return left === null || right === null
    ? left === right
    : left.stage === right.stage
      && left.step === right.step
      && left.date === right.date
      && left.time === right.time
      && left.note === right.note;
}

function sameExtraction(
  left: ReviewRecordExtraction | null,
  right: ReviewRecordExtraction,
): boolean {
  if (left === null) return false;
  const sameHistory = left.history === null || right.history === null
    ? left.history === right.history
    : left.history.step === right.history.step
      && left.history.date === right.history.date
      && left.history.outcome === right.history.outcome
      && left.history.summary === right.history.summary;
  const sameProjectedState = left.projected_state === null || right.projected_state === null
    ? left.projected_state === right.projected_state
    : left.projected_state.stage === right.projected_state.stage
      && left.projected_state.current_step === right.projected_state.current_step;
  const sameQuestions = left.questions.length === right.questions.length
    && left.questions.every((question, index) => {
      const other = right.questions[index];
      return other !== undefined
        && question.text === other.text
        && question.stuck === other.stuck
        && question.knowledge_points.length === other.knowledge_points.length
        && question.knowledge_points.every((point, pointIndex) => (
          point === other.knowledge_points[pointIndex]
        ));
    });
  return left.company === right.company
    && left.position === right.position
    && left.channel === right.channel
    && sameHistory
    && sameProjectedState
    && left.clear_next_action === right.clear_next_action
    && sameNextAction(left.next_action, right.next_action)
    && sameQuestions
    && left.mood === right.mood
    && left.time_of_day === right.time_of_day
    && left.factors.length === right.factors.length
    && left.factors.every((factor, index) => factor === right.factors[index]);
}

function extractionChanged(operation: ReviewRecordOperation, extraction: ReviewRecordExtraction): boolean {
  return !sameExtraction(operation.preview?.extraction ?? null, extraction);
}

function sameOperationIds(left: ReadonlySet<string>, right: ReadonlySet<string>): boolean {
  return left.size === right.size && [...left].every((operationId) => right.has(operationId));
}

function extractionEditIssue(
  extraction: ReviewRecordExtraction,
  t: Localize,
  currentState?: {
    stage: ReviewRecordApplicationStage;
    current_step: string | null;
    next_action: ReviewRecordExtraction["next_action"];
  },
): string | null {
  if (extraction.history === null && extraction.projected_state === null
      && !extraction.clear_next_action && extraction.next_action === null) {
    return t("至少保留一项：本次发生、确认后的阶段与环节或下一步安排。", "Keep at least one item: what happened, the resulting stage and step, or the next action.");
  }
  if (extraction.history !== null
      && !extraction.history.step?.trim()
      && extraction.history.outcome === null
      && !extraction.history.summary?.trim()) {
    return t("本次发生至少需要具体环节、结果或说明。", "What happened needs at least a specific step, outcome, or note.");
  }
  if (extraction.projected_state !== null
      && extraction.projected_state.stage === null
      && !extraction.projected_state.current_step?.trim()) {
    return t("确认后的阶段与环节至少需要填写一项。", "Enter at least a resulting stage or current step.");
  }
  if (extraction.projected_state !== null && currentState) {
    const projectedStage = extraction.projected_state.stage ?? currentState.stage;
    const projectedStep = normalizedText(extraction.projected_state.current_step)
      ?? currentState.current_step;
    if (projectedStage === currentState.stage && projectedStep === currentState.current_step) {
      return t("确认后的阶段与环节和岗位当前值相同；请取消这一项或填写实际变化。", "The resulting stage and step match the current values. Remove this item or enter the actual change.");
    }
  }
  const finalStage = extraction.projected_state?.stage ?? currentState?.stage;
  const closesExistingPlan = (finalStage === "withdrawn" || finalStage === "rejected")
    && currentState?.next_action !== null;
  if (extraction.clear_next_action && extraction.next_action !== null) {
    return t("清空现有下一步与设置新下一步不能同时进行。", "You cannot clear the existing action and set a new one at the same time.");
  }
  if (extraction.clear_next_action && currentState?.next_action === null) {
    return t("当前没有可清空的下一步安排；请选择保持现有安排。", "There is no existing next action to clear; choose to preserve it.");
  }
  if ((finalStage === "withdrawn" || finalStage === "rejected")
      && extraction.next_action !== null) {
    return t("流程结束后不能保留下一步；请删除下一步安排或调整阶段。", "A closed process cannot retain a next action. Remove it or change the stage.");
  }
  if (closesExistingPlan && !extraction.clear_next_action) {
    return t("结束跟进后会清空现有下一步；请保持“清空现有安排”。", "Ending follow-up clears the existing next action; keep “Clear existing action” selected.");
  }
  if (extraction.next_action !== null && !extraction.next_action.step.trim()) {
    return t("下一步名称不能为空。", "The next-action name cannot be empty.");
  }
  if (extraction.next_action?.time && !extraction.next_action.date) {
    return t("下一步设置时间时必须同时设置日期。", "A next-action time requires a date.");
  }
  return null;
}

function ExtractionEditor({
  extraction,
  currentStage,
  currentStep,
  hasCurrentNextAction,
  identityLocked,
  onChange,
}: {
  extraction: ReviewRecordExtraction;
  currentStage: ReviewRecordApplicationStage;
  currentStep: string | null;
  hasCurrentNextAction: boolean;
  identityLocked: boolean;
  onChange: (next: ReviewRecordExtraction) => void;
}) {
  const t = useLocalizer();
  const english = t("zh", "en") === "en";
  const stages = english ? STAGES_EN : STAGES;
  const updateHistory = (patch: Partial<NonNullable<ReviewRecordExtraction["history"]>>) => {
    if (extraction.history) onChange({ ...extraction, history: { ...extraction.history, ...patch } });
  };
  const updateState = (patch: Partial<NonNullable<ReviewRecordExtraction["projected_state"]>>) => {
    if (extraction.projected_state) {
      onChange({ ...extraction, projected_state: { ...extraction.projected_state, ...patch } });
    }
  };
  const updateNext = (patch: Partial<NonNullable<ReviewRecordExtraction["next_action"]>>) => {
    if (extraction.next_action) onChange({ ...extraction, next_action: { ...extraction.next_action, ...patch } });
  };
  const projectedStage = extraction.projected_state?.stage ?? currentStage;
  const nextActionDefaultStage = projectedStage;
  const terminalProjection = projectedStage === "withdrawn" || projectedStage === "rejected";
  const terminalPlanMustClear = terminalProjection && hasCurrentNextAction;

  return (
    <div className="grid gap-3">
      <section aria-label={t("岗位身份", "Role identity")} className="rounded-lg border border-line bg-panel p-3">
        <p className="mb-2 text-xs font-semibold">{t("岗位", "Role")}</p>
        <div className="grid gap-2 sm:grid-cols-3">
          <label className="text-xs text-ink-3">{t("公司", "Company")}<input aria-label={t("公司", "Company")} value={extraction.company ?? ""} maxLength={200} disabled={identityLocked} onChange={(event) => onChange({ ...extraction, company: event.target.value || null })} className="input mt-1 w-full" /></label>
          <label className="text-xs text-ink-3">{t("岗位", "Role")}<input aria-label={t("岗位", "Role")} value={extraction.position ?? ""} maxLength={300} disabled={identityLocked} onChange={(event) => onChange({ ...extraction, position: event.target.value || null })} className="input mt-1 w-full" /></label>
          <label className="text-xs text-ink-3">{t("渠道（可选）", "Channel (optional)")}<input aria-label={t("渠道", "Channel")} value={extraction.channel ?? ""} maxLength={100} onChange={(event) => onChange({ ...extraction, channel: event.target.value || null })} className="input mt-1 w-full" /></label>
        </div>
        {identityLocked && (
          <p className="mt-2 text-xs text-ink-3">
            {t("本次确认已锁定岗位，避免把进展误写到另一岗位；若识别错误，请取消纳入后重新复盘。", "This confirmation locks the role to prevent writing progress elsewhere. If it is wrong, exclude it and review again.")}
          </p>
        )}
      </section>

      <section aria-label={t("本次发生", "What happened")} className="rounded-lg border border-line bg-panel p-3">
        <label className="flex items-center gap-2 text-xs font-semibold">
          <input type="checkbox" checked={extraction.history !== null} onChange={(event) => onChange({
            ...extraction,
            history: event.target.checked ? { step: null, date: null, outcome: null, summary: null } : null,
          })} />
          {t("本次发生", "What happened")}
        </label>
        {extraction.history && (
          <div className="mt-2 grid gap-2 sm:grid-cols-3">
            <label className="text-xs text-ink-3">{t("具体环节", "Specific step")}<input aria-label={t("本次发生的具体环节", "Specific step that occurred")} value={extraction.history.step ?? ""} maxLength={300} onChange={(event) => updateHistory({ step: event.target.value || null })} placeholder={t("例如：二面", "For example: second interview")} className="input mt-1 w-full" /></label>
            <label className="text-xs text-ink-3">{t("发生日期", "Date")}<input type="date" aria-label={t("本次发生日期", "Date this occurred")} value={extraction.history.date ?? ""} onChange={(event) => updateHistory({ date: event.target.value || null })} className="input mt-1 w-full" /></label>
            <label className="text-xs text-ink-3">{t("结果（可选）", "Outcome (optional)")}<select aria-label={t("本次发生结果", "Outcome")} value={extraction.history.outcome ?? ""} onChange={(event) => updateHistory({ outcome: (event.target.value || null) as NonNullable<ReviewRecordExtraction["history"]>["outcome"] })} className="input mt-1 w-full"><option value="">{t("未设置", "Not set")}</option><option value="passed">{t("通过", "Passed")}</option><option value="failed">{t("未通过", "Not passed")}</option><option value="cancelled">{t("取消", "Cancelled")}</option></select></label>
            <label className="text-xs text-ink-3 sm:col-span-3">{t("说明（可选）", "Notes (optional)")}<textarea aria-label={t("本次发生说明", "What happened notes")} value={extraction.history.summary ?? ""} maxLength={2_000} rows={2} onChange={(event) => updateHistory({ summary: event.target.value || null })} className="input mt-1 min-h-16 w-full resize-y" /></label>
          </div>
        )}
      </section>

      <section aria-label={t("确认后的阶段与环节", "Resulting stage and step")} className="rounded-lg border border-line bg-panel p-3">
        <label className="flex items-center gap-2 text-xs font-semibold">
          <input type="checkbox" checked={extraction.projected_state !== null} onChange={(event) => onChange({
            ...extraction,
            projected_state: event.target.checked ? { stage: null, current_step: null } : null,
          })} />
          {t("确认后的阶段与环节", "Resulting stage and step")}
        </label>
        {extraction.projected_state && (
          <div className="mt-2 grid gap-2 sm:grid-cols-2">
            <label className="text-xs text-ink-3">{t("阶段", "Stage")}<select aria-label={t("复盘后的阶段", "Stage after review")} value={extraction.projected_state.stage ?? ""} onChange={(event) => {
              const stage = (event.target.value || null) as ReviewRecordApplicationStage | null;
              onChange({
                ...extraction,
                projected_state: { ...extraction.projected_state!, stage },
                ...(stage === "withdrawn" || stage === "rejected"
                  ? { clear_next_action: hasCurrentNextAction, next_action: null }
                  : {}),
              });
            }} className="input mt-1 w-full"><option value="">{t("保持当前阶段", "Keep current stage")}</option>{stages.map(([stage, label]) => <option key={stage} value={stage}>{label}</option>)}</select></label>
            <label className="text-xs text-ink-3">{t("当前环节", "Current step")}<input aria-label={t("复盘后的当前环节", "Current step after review")} value={extraction.projected_state.current_step ?? ""} maxLength={300} onChange={(event) => updateState({ current_step: event.target.value || null })} placeholder={currentStep ? t(`留空保持「${currentStep}」`, `Leave blank to keep “${currentStep}”`) : t("只填写已经确认到达的环节；留空保持未设置", "Enter only a confirmed step; leave blank to keep it unset")} className="input mt-1 w-full" /></label>
          </div>
        )}
      </section>

      <section aria-label={t("下一步安排", "Next action")} className="rounded-lg border border-line bg-panel p-3">
        <label className="block text-xs font-semibold">{t("下一步安排", "Next action")}
          <select aria-label={t("下一步安排处理方式", "Next-action handling")} value={extraction.next_action !== null ? "set" : extraction.clear_next_action ? "clear" : "preserve"} onChange={(event) => {
            const mode = event.target.value;
            onChange({
              ...extraction,
              clear_next_action: mode === "clear",
              next_action: mode === "set"
                ? { stage: nextActionDefaultStage, step: "", date: null, time: null, note: null }
                : null,
            });
          }} className="input mt-1 w-full font-normal">
            <option value="preserve" disabled={terminalPlanMustClear}>{t("保持现有安排", "Keep existing action")}</option>
            <option value="clear" disabled={!hasCurrentNextAction}>{t("清空现有安排", "Clear existing action")}</option>
            <option value="set" disabled={terminalProjection}>{t("设置或替换安排", "Set or replace action")}</option>
          </select>
        </label>
        {terminalPlanMustClear && (
          <p className="mt-1 text-xs text-ink-3">{t("流程结束后不能保留未来安排，本次会明确清空现有下一步。", "A closed process cannot retain future actions; this update will explicitly clear the current one.")}</p>
        )}
        {extraction.next_action && (
          <div className="mt-2 grid gap-2 sm:grid-cols-2">
            <label className="text-xs text-ink-3">{t("完成后阶段（可不变）", "Stage after completion (optional)")}<select aria-label={t("下一步完成后阶段", "Stage after next action")} value={extraction.next_action.stage} onChange={(event) => updateNext({ stage: event.target.value as ReviewRecordApplicationStage })} className="input mt-1 w-full">{stages.map(([stage, label]) => <option key={stage} value={stage}>{label}</option>)}</select></label>
            <label className="text-xs text-ink-3">{t("下一步", "Next action")} <span className="text-bad">*</span><input aria-label={t("下一步", "Next action")} value={extraction.next_action.step} maxLength={300} onChange={(event) => updateNext({ step: event.target.value })} placeholder={t("例如：终面、等待结果", "For example: final interview, await decision")} className="input mt-1 w-full" /></label>
            <label className="text-xs text-ink-3">{t("日期（可选）", "Date (optional)")}<input type="date" aria-label={t("下一步日期", "Next-action date")} value={extraction.next_action.date ?? ""} onChange={(event) => updateNext({ date: event.target.value || null, time: event.target.value ? extraction.next_action?.time ?? null : null })} className="input mt-1 w-full" /></label>
            <label className="text-xs text-ink-3">{t("时间（可选）", "Time (optional)")}<input type="time" aria-label={t("下一步时间", "Next-action time")} value={extraction.next_action.time ?? ""} disabled={!extraction.next_action.date} onChange={(event) => updateNext({ time: event.target.value || null })} className="input mt-1 w-full" /></label>
            <label className="text-xs text-ink-3 sm:col-span-2">{t("说明（可选）", "Notes (optional)")}<textarea aria-label={t("下一步说明", "Next-action notes")} value={extraction.next_action.note ?? ""} maxLength={2_000} rows={2} onChange={(event) => updateNext({ note: event.target.value || null })} className="input mt-1 min-h-16 w-full resize-y" /></label>
          </div>
        )}
      </section>
    </div>
  );
}

export function ReviewRecordProposalBatchCard({
  operations,
  actionsDisabled,
  readUncertain,
  action,
  recoveryPending,
  errors,
  onDecide,
}: {
  operations: ReviewRecordOperation[];
  actionsDisabled: boolean;
  readUncertain: boolean;
  action: "approve" | "reject" | null;
  recoveryPending: boolean;
  errors: Record<string, string>;
  onDecide: (decisions: ReviewRecordProposalBatchDecision[]) => void;
}) {
  const t = useLocalizer();
  const { locale } = useLocale();
  const english = t("zh", "en") === "en";
  const [excludedOperationIds, setExcludedOperationIds] = useState<Set<string>>(() => new Set());
  const [editingOperationIds, setEditingOperationIds] = useState<Set<string>>(() => new Set());
  const [editedExtractions, setEditedExtractions] = useState<Record<string, ReviewRecordExtraction>>({});
  const operationIds = useMemo(() => new Set(operations.map((item) => item.operation_id)), [operations]);
  const operationIdKey = operations.map((item) => item.operation_id).join("\u0000");

  useEffect(() => {
    setExcludedOperationIds((current) => {
      const next = new Set([...current].filter((id) => operationIds.has(id)));
      return sameOperationIds(current, next) ? current : next;
    });
    setEditingOperationIds((current) => {
      const next = new Set([...current].filter((id) => operationIds.has(id)));
      return sameOperationIds(current, next) ? current : next;
    });
    setEditedExtractions((current) => Object.fromEntries(
      Object.entries(current).filter(([id]) => operationIds.has(id)),
    ));
  }, [operationIdKey, operationIds]);

  const includedOperationIds = new Set(operations.map((item) => item.operation_id).filter((id) => !excludedOperationIds.has(id)));
  const effectiveExtractions = new Map(Object.entries(editedExtractions));
  const counts = countReviewRecordProposalBatch(operations, includedOperationIds, effectiveExtractions);
  const createCount = operations.filter(
    (operation) => operation.preview?.target_plan?.kind !== "existing",
  ).length;
  const updateCount = operations.length - createCount;
  const disabled = actionsDisabled || readUncertain || action !== null || recoveryPending;
  const changedExtractions = new Map<string, ReviewRecordExtraction>(operations.flatMap((operation) => {
    const edited = editedExtractions[operation.operation_id];
    return edited !== undefined && extractionChanged(operation, edited)
      ? [[operation.operation_id, edited] as const]
      : [];
  }));

  function decide(rejectAll: boolean) {
    onDecide(buildReviewRecordProposalBatchDecisions(
      operations,
      includedOperationIds,
      rejectAll,
      changedExtractions,
    ));
  }

  return (
    <section aria-labelledby={`review-record-batch-title-${operations[0]?.client_turn_id ?? "empty"}`} className="flex w-full flex-col gap-2.5">
      <div className="rounded-2xl border border-warn/30 bg-warn-soft px-4 py-3 shadow-card">
        <h2 id={`review-record-batch-title-${operations[0]?.client_turn_id ?? "empty"}`} className="text-sm font-medium text-ink-1">{t(`本轮共 ${operations.length} 条岗位等待确认`, `${operations.length} roles await confirmation in this batch`)}</h2>
        <p className="mt-1 text-xs leading-5 text-warn">{t(`新增 ${createCount} · 更新 ${updateCount} · 请核对下方岗位后统一处理；未确认前不会写入。`, `${createCount} new · ${updateCount} updates · Review the roles below, then process them together. Nothing is written before you confirm.`)}</p>
        <div className="mt-3 flex flex-wrap items-center justify-between gap-2">
          <span className="text-xs text-ink-3" aria-live="polite">{t(`已选择 ${counts.includedCount} / ${operations.length} 条`, `${counts.includedCount} / ${operations.length} selected`)}{counts.includedCount > 0 && <> · {t(`写入 ${counts.publishCount} 条 · 保留草稿 ${counts.retainedDraftCount} 条`, `${counts.publishCount} to write · ${counts.retainedDraftCount} drafts`)}</>}{counts.excludedCount > 0 && <> · {t(`放弃 ${counts.excludedCount} 条`, `${counts.excludedCount} discarded`)}</>}{editingOperationIds.size > 0 && <> · {t(`正在编辑 ${editingOperationIds.size} 条`, `${editingOperationIds.size} being edited`)}</>}</span>
          <div className="flex items-center gap-2">
            <button type="button" className="btn btn-sm btn-danger" onClick={() => decide(true)} disabled={disabled}>{action === "reject" ? t("正在处理…", "Processing…") : t("全部不写入", "Write none")}</button>
            <button type="button" className="btn-primary btn-sm" onClick={() => decide(false)} disabled={disabled || counts.includedCount === 0 || editingOperationIds.size > 0}>{action === "approve" ? t("正在处理…", "Processing…") : editingOperationIds.size > 0 ? t(`请先完成 ${editingOperationIds.size} 项编辑`, `Finish ${editingOperationIds.size} edits first`) : t(`确认处理 ${counts.includedCount} 条`, `Confirm ${counts.includedCount}`)}</button>
          </div>
        </div>
        {recoveryPending && !operations.some((operation) => Boolean(errors[operation.operation_id])) && <p role="status" className="mt-2 text-xs text-warn">{t("上次统一确认的结果仍在核对；页面只会继续同一次选择。", "The previous batch confirmation is still being checked; this page will only continue that same choice.")}</p>}
      </div>
      <fieldset disabled={disabled} className="space-y-2.5">
        <legend className="sr-only">{t("选择要写入的求职进展", "Select progress to record")}</legend>
        {operations.map((operation) => {
          const included = includedOperationIds.has(operation.operation_id);
          const editing = editingOperationIds.has(operation.operation_id);
          const edited = editedExtractions[operation.operation_id];
          const changed = edited !== undefined && extractionChanged(operation, edited);
          const issue = edited ? extractionEditIssue(
            edited,
            t,
            operation.preview?.target_plan ? {
              stage: operation.preview.target_plan.current_stage,
              current_step: operation.preview.target_plan.current_step,
              next_action: operation.preview.target_plan.current_next_action,
            } : undefined,
          ) : null;
          const shown = edited ?? operation.preview?.extraction ?? null;
          const targetPlan = operation.preview?.target_plan ?? null;
          const company = targetPlan?.company ?? shown?.company ?? t("公司未提供", "Company not provided");
          const position = targetPlan?.position ?? shown?.position ?? t("岗位未提供", "Role not provided");
          const finalStage = changed
            ? shown?.projected_state?.stage ?? targetPlan?.current_stage ?? null
            : targetPlan?.projected_stage ?? shown?.projected_state?.stage ?? null;
          const finalStep = changed
            ? normalizedText(shown?.projected_state?.current_step ?? null)
              ?? targetPlan?.current_step
              ?? null
            : targetPlan?.projected_step
              ?? normalizedText(shown?.projected_state?.current_step ?? null)
              ?? null;
          const finalChannel = changed
            ? normalizedText(shown?.channel ?? null) ?? targetPlan?.current_channel ?? null
            : targetPlan?.projected_channel ?? normalizedText(shown?.channel ?? null) ?? null;
          const finalAppliedDate = changed
            ? targetPlan?.current_applied_date
              ?? (targetPlan?.current_stage !== "applied" && finalStage === "applied"
                ? shown?.history?.date ?? null
                : null)
            : targetPlan?.projected_applied_date ?? null;
          const finalNextAction = finalStage === "withdrawn" || finalStage === "rejected"
            ? null
            : changed
              ? shown?.clear_next_action
                ? null
                : shown?.next_action ?? targetPlan?.current_next_action ?? null
              : targetPlan?.projected_next_action ?? shown?.next_action ?? null;
          const eventText = shown?.history === null || shown?.history === undefined
            ? t("没有新增历程", "No new history entry")
            : [
                shown.history.step ?? shown.history.summary ?? t("已记录事实", "Fact recorded"),
                shown.history.outcome === "passed"
                  ? t("通过", "Passed")
                  : shown.history.outcome === "failed"
                    ? t("未通过", "Not passed")
                    : shown.history.outcome === "cancelled"
                      ? t("取消", "Cancelled")
                      : null,
                shown.history.date,
              ].filter(Boolean).join(" · ");
          const nextActionText = finalNextAction === null
            ? t("未设置", "Not set")
            : [
                finalNextAction.step,
                finalNextAction.date,
                finalNextAction.time,
                english
                  ? `then ${stageLabel(finalNextAction.stage, true)}`
                  : `完成后进入${stageLabel(finalNextAction.stage)}`,
              ].filter(Boolean).join(" · ");
          const createdLabel = formatDate(operation.created_time, locale, "dateTime")
            || operation.created_time;
          const isExisting = targetPlan?.kind === "existing";
          return (
            <ProposalDecisionCard
              key={operation.operation_id}
              id={`review-record-batch-${operation.operation_id}`}
              tone="info"
              dimmed={!included}
              title={<>{isExisting ? t("准备修改岗位", "Prepare to update role") : t("准备新增岗位", "Prepare to add role")} · {company} · {position}{changed && <span className="ml-2 text-xs font-normal text-info">{t("已编辑", "Edited")}</span>}</>}
              description={included
                ? isExisting
                  ? t("确认后会按下方信息更新这条岗位；未确认前保持现状。", "Confirmation updates this role with the information below; nothing changes before then.")
                  : t("确认后会按下方信息新增这条岗位；未确认前不会写入。", "Confirmation adds this role with the information below; nothing is saved before then.")
                : t("这条岗位已从本批处理中去除；点击恢复可重新纳入。", "This role is excluded from the batch. Restore it to include it again.")}
              timeLabel={createdLabel}
              actions={<>
                <button type="button" className="btn btn-sm bg-panel" disabled={!included} onClick={() => {
                  if (!operation.preview) return;
                  setEditedExtractions((current) => current[operation.operation_id]
                    ? current
                    : { ...current, [operation.operation_id]: operation.preview!.extraction });
                  setEditingOperationIds((current) => new Set(current).add(operation.operation_id));
                }}>
                  {editing ? t("正在编辑", "Editing") : changed ? t("继续编辑", "Continue editing") : t("编辑", "Edit")}
                </button>
                <button type="button" className="btn btn-sm bg-panel" onClick={() => {
                  setExcludedOperationIds((current) => {
                    const next = new Set(current);
                    if (included) next.add(operation.operation_id);
                    else next.delete(operation.operation_id);
                    return next;
                  });
                  if (included) setEditingOperationIds((current) => {
                    const next = new Set(current);
                    next.delete(operation.operation_id);
                    return next;
                  });
                }}>
                  {included ? t("去除", "Remove") : t("恢复", "Restore")}
                </button>
              </>}
              error={errors[operation.operation_id] ?? null}
              supplement={editing && edited ? (
                <div className="mt-3 rounded-xl border border-info/25 bg-info-soft/60 p-3">
                  <ExtractionEditor
                    extraction={edited}
                    currentStage={targetPlan?.current_stage ?? "backlog"}
                    currentStep={targetPlan?.current_step ?? null}
                    hasCurrentNextAction={targetPlan?.current_next_action != null}
                    identityLocked={targetPlan != null}
                    onChange={(next) => setEditedExtractions((current) => ({
                      ...current,
                      [operation.operation_id]: next,
                    }))}
                  />
                  {issue && <p role="alert" className="mt-2 text-xs font-medium text-bad">{issue}</p>}
                  <div className="mt-3 flex flex-wrap justify-end gap-2">
                    <button type="button" className="btn btn-sm" onClick={() => {
                      setEditingOperationIds((current) => {
                        const next = new Set(current);
                        next.delete(operation.operation_id);
                        return next;
                      });
                      setEditedExtractions((current) => {
                        const next = { ...current };
                        delete next[operation.operation_id];
                        return next;
                      });
                    }}>{t("恢复识别结果", "Restore extracted result")}</button>
                    <button type="button" className="btn-primary btn-sm" disabled={issue !== null} onClick={() => {
                      setEditedExtractions((current) => ({
                        ...current,
                        [operation.operation_id]: normalizeEditedExtraction(current[operation.operation_id]),
                      }));
                      setEditingOperationIds((current) => {
                        const next = new Set(current);
                        next.delete(operation.operation_id);
                        return next;
                      });
                    }}>{t("完成编辑", "Finish editing")}</button>
                  </div>
                </div>
              ) : undefined}
            >
              <dl className="grid gap-x-5 gap-y-3 sm:grid-cols-2">
                <div>
                  <dt className="text-ink-3">{t("公司与岗位", "Company and role")}</dt>
                  <dd className="mt-0.5 break-words font-medium text-ink">{company} · {position}</dd>
                </div>
                <div>
                  <dt className="text-ink-3">{t("确认后的阶段与环节", "Stage and step after confirmation")}</dt>
                  <dd className="mt-0.5 break-words">{stageLabel(finalStage, english)} · {finalStep || t("未设置", "Not set")}</dd>
                </div>
                <div>
                  <dt className="text-ink-3">{t("投递日期与渠道", "Applied date and channel")}</dt>
                  <dd className="mt-0.5 break-words">{finalAppliedDate || t("未填写", "Not set")} · {finalChannel || t("未填写", "Not set")}</dd>
                </div>
                <div>
                  <dt className="text-ink-3">{t("本次发生", "What happened")}</dt>
                  <dd className="mt-0.5 break-words">{eventText}</dd>
                </div>
                <div className="sm:col-span-2">
                  <dt className="text-ink-3">{t("下一步", "Next action")}</dt>
                  <dd className="mt-0.5 break-words">{nextActionText}</dd>
                </div>
              </dl>
            </ProposalDecisionCard>
          );
        })}
      </fieldset>
    </section>
  );
}
