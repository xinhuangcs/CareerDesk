import { useCallback, useEffect, useRef, useState } from "react";
import { formatDate } from "../../i18n/formatters";
import { useLocale } from "../../i18n/localePreference";
import { useLocalizer } from "../../i18n/useLocalizer";
import type { UiLocale } from "../../i18n/i18n";

import type { ApplicationStage } from "../applications/applicationContract";
import {
  approveIntakeOperation,
  getIntakeOperation,
  getPendingIntakeOperations,
  rejectIntakeOperation,
} from "./intakeOperationApi";
import type { IntakeOperation } from "./intakeOperationContract";
import {
  mergePendingIntakeOperations,
  reconcileIntakeExcludedRows,
  retainIntakeOperationErrors,
} from "./intakeOperationRefresh";
import {
  operationAnchorElement,
  renderOperationAtAnchor,
  type OperationAnchorIdResolver,
} from "../operations/operationAnchorPortal";
import {
  useOperationRefreshScheduler,
  type OperationRefreshBatch,
} from "../operations/useOperationRefreshScheduler.ts";

export type IntakeAction = "approve" | "reject";
type IntakeNotice = {
  kind: "status" | "alert";
  message: string;
  preserveWithPending?: boolean;
  operationId?: string;
};
export type IntakeCommandComparison = "same" | "different" | "non_terminal";

const STAGE_LABELS: Record<ApplicationStage, string> = {
  backlog: "待定",
  applied: "已投递",
  written_test: "笔试中",
  interviewing: "面试中",
  offer: "Offer",
  withdrawn: "不再跟进",
  rejected: "已挂",
  pooled: "泡池子",
};

function normalizeExcludedRows(rows: number[]): number[] {
  return [...new Set(rows)].sort((left, right) => left - right);
}

/** Compare terminal state with this tab's exact command so cross-tab conflicts are not reported as success. */
export function compareIntakeCommand(
  requestedAction: IntakeAction,
  requestedExcludedRows: number[],
  canonical: IntakeOperation,
): IntakeCommandComparison {
  if (canonical.state === "rejected") {
    return requestedAction === "reject" ? "same" : "different";
  }
  if (canonical.state !== "completed") return "non_terminal";
  if (requestedAction !== "approve" || !Array.isArray(canonical.exclude_indexes)) {
    return "different";
  }
  const requested = normalizeExcludedRows(requestedExcludedRows);
  const actual = normalizeExcludedRows(canonical.exclude_indexes);
  return requested.length === actual.length
    && requested.every((rowNumber, index) => rowNumber === actual[index])
    ? "same"
    : "different";
}

function formatOperationTime(value: string, locale: UiLocale): string {
  return formatDate(value, locale, "dateTime");
}

function completedOperationMessage(operation: IntakeOperation, locale: UiLocale = "zh-CN"): string {
  const created = Array.isArray(operation.result?.created) ? operation.result.created.length : null;
  const updated = Array.isArray(operation.result?.updated) ? operation.result.updated.length : null;
  if (created !== null && updated !== null) {
    return locale === "en"
      ? `Saved ${created + updated} role changes (${created} created, ${updated} updated). View them in Application Tracker.`
      : `已保存 ${created + updated} 条岗位变更（新增 ${created}，更新 ${updated}）。可前往“求职进展”查看。`;
  }
  return locale === "en"
    ? "Role changes saved. View the result in Application Tracker."
    : "岗位变更已保存。可前往“求职进展”查看结果。";
}

function IntakeOperationCard({
  operation,
  excludedRowNumbers,
  action,
  actionsDisabled,
  error,
  onToggle,
  onApprove,
  onReject,
}: {
  operation: IntakeOperation;
  excludedRowNumbers: number[];
  action: IntakeAction | null;
  actionsDisabled: boolean;
  error: string | null;
  onToggle: (rowNumber: number, included: boolean) => void;
  onApprove: () => void;
  onReject: () => void;
}) {
  const l = useLocalizer();
  const { locale } = useLocale();
  const excluded = new Set(excludedRowNumbers);
  const includedCount = operation.positions.length - excluded.size;
  const createCount = operation.positions.filter((position) => position.mode === "create").length;
  const updateCount = operation.positions.length - createCount;
  const createdLabel = formatOperationTime(operation.created_time, locale);

  return (
    <section
      aria-labelledby={`intake-operation-${operation.operation_id}`}
      className="card overflow-hidden"
    >
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-line-2 px-4 py-3.5">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="flex h-6 w-6 items-center justify-center rounded-lg bg-warn-soft text-warn" aria-hidden>
              <svg viewBox="0 0 16 16" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M8 2.2 13.4 13H2.6zM8 6v3.2M8 11.5v.1" /></svg>
            </span>
            <h2 id={`intake-operation-${operation.operation_id}`} className="text-sm font-semibold">
              {l(`确认岗位信息 · ${operation.positions.length} 条`, `Confirm role information · ${operation.positions.length} items`)}
            </h2>
          </div>
          <p className="ml-8 mt-1 text-xs text-ink-3">{l(`新增 ${createCount} · 更新 ${updateCount} · 已选 ${includedCount} · 仅展示将保存的内容`, `${createCount} new · ${updateCount} updates · ${includedCount} selected · only saved content is shown`)}</p>
          {operation.source_rows > 0 && (
            <p className="ml-8 mt-1 text-xs text-ink-3">
              {l(`已读取 ${operation.source_rows} 行${operation.skipped_rows > 0 ? ` · 略过 ${operation.skipped_rows} 行` : ""}`, `Read ${operation.source_rows} rows${operation.skipped_rows > 0 ? ` · skipped ${operation.skipped_rows}` : ""}`)}
            </p>
          )}
        </div>
        {createdLabel && (
          <span className="shrink-0 text-xs tabular-nums text-ink-3">{createdLabel}</span>
        )}
      </div>

      <fieldset
        disabled={actionsDisabled}
        className="max-h-[min(46vh,32rem)] divide-y divide-line-2 overflow-y-auto overscroll-contain scroll-smooth [scrollbar-gutter:stable]"
      >
        <legend className="sr-only">{l("选择要执行的岗位变更", "Choose role changes to apply")}</legend>
        {operation.positions.map((position, index) => {
          const rowNumber = index + 1; // The proposal contract uses one-based row numbers in exclude_indexes.
          const included = !excluded.has(rowNumber);
          const checkboxId = `intake-${operation.operation_id}-${rowNumber}`;
          const facts = [
            [l("部门", "Department"), position.department],
            [l("渠道", "Channel"), position.channel],
            [l("阶段", "Stage"), locale === "en" ? ({ backlog: "Considering", applied: "Applied", written_test: "Assessment", interviewing: "Interviewing", offer: "Offer", withdrawn: "Withdrawn", rejected: "Rejected", pooled: "On hold" } as const)[position.stage] : STAGE_LABELS[position.stage]],
            [l("当前环节", "Current step"), position.current_step],
            [l("投递日期", "Application date"), position.applied_date],
            [l("泡池原因", "Reason for pooling"), position.pause_reason],
            [l("优先级", "Priority"), position.priority === "high" ? l("高", "High") : position.priority === "medium" ? l("中", "Medium") : position.priority === "low" ? l("低", "Low") : null],
          ].filter((fact): fact is [string, string] => Boolean(fact[1]));
          return (
            <div
              key={`${index}-${position.company}-${position.position}`}
              className={`flex items-start gap-3 px-4 py-3.5 transition-colors hover:bg-panel-2/35 ${included ? "" : "opacity-50"}`}
            >
              <input
                id={checkboxId}
                type="checkbox"
                checked={included}
                onChange={(event) => onToggle(rowNumber, event.target.checked)}
                aria-label={l(`执行${position.mode === "create" ? "新增" : "更新"} ${position.company} ${position.position}`, `${position.mode === "create" ? "Create" : "Update"} ${position.company} ${position.position}`)}
                className="mt-0.5 h-4 w-4 shrink-0 accent-accent"
              />
              <div className="min-w-0 flex-1">
                <label
                  htmlFor={checkboxId}
                  className="flex cursor-pointer flex-wrap items-baseline gap-x-2 gap-y-1"
                >
                  <span className="min-w-0 max-w-full break-words text-sm font-medium">
                    {position.company}
                  </span>
                  <span className="min-w-0 max-w-full break-words text-sm text-ink-2">
                    {position.position}
                  </span>
                  <span className={position.mode === "create"
                    ? "tag bg-ok-soft text-ok"
                    : "tag bg-info-soft text-info"}
                  >
                    {position.mode === "create" ? l("新增记录", "New record") : l("更新已有记录", "Update existing record")}
                  </span>
                </label>

                <dl className="mt-2 flex flex-wrap gap-1.5 text-xs">
                  {facts.map(([label, value]) => (
                    <div key={label} className="inline-flex max-w-full items-baseline gap-1 rounded-lg bg-panel-2 px-2 py-1">
                      <dt className="shrink-0 text-ink-3">{label}</dt>
                      <dd className="min-w-0 break-words text-ink-2">{value}</dd>
                    </div>
                  ))}
                  {position.next_action && (
                    <div className="inline-flex max-w-full items-baseline gap-1 rounded-lg bg-info-soft px-2 py-1">
                      <dt className="shrink-0 text-info">{l("下一步", "Next action")}</dt>
                      <dd className="min-w-0 break-words text-ink-2">
                        {[position.next_action.step, position.next_action.date, position.next_action.time]
                          .filter(Boolean).join(" · ")}
                      </dd>
                    </div>
                  )}
                </dl>

                {position.next_action?.note && (
                  <p className="mt-2 whitespace-pre-wrap break-words text-xs leading-5 text-ink-2">{position.next_action.note}</p>
                )}

                {position.application_note?.trim() && (
                  <details className="mt-2 rounded-lg bg-panel-2 px-3 py-2 text-xs">
                    <summary className="cursor-pointer font-medium text-ink-2">{l("查看岗位备注", "View role notes")}</summary>
                    <div className="mt-2 whitespace-pre-wrap break-words border-t border-line pt-2 leading-relaxed text-ink-2">
                      {position.application_note}
                    </div>
                  </details>
                )}

                {(position.flags.clear_next_action
                  || position.flags.invalidate_prep
                  || position.flags.add_applied_entry) && (
                  <aside
                    aria-label={l("执行影响", "Effects of applying")}
                    className="mt-2 flex flex-wrap gap-x-3 gap-y-1 rounded-lg bg-warn-soft px-3 py-2 text-xs text-warn"
                  >
                    <p className="font-medium">{l("同时：", "Also:")}</p>
                    <ul className="flex flex-wrap gap-x-3">
                      {position.flags.clear_next_action && <li>{l("清除下一步", "Clear next action")}</li>}
                      {position.flags.invalidate_prep && (
                        <li>{l("旧调研需重新生成", "Regenerate old research")}</li>
                      )}
                      {position.flags.add_applied_entry && (
                        <li>{l("补记投递历程", "Add application history")}</li>
                      )}
                    </ul>
                  </aside>
                )}

                {(position.skills.length > 0 || position.highlights.length > 0) && (
                  <div className="mt-2 flex flex-wrap gap-1.5 text-xs">
                    {position.skills.map((skill, skillIndex) => (
                            <span
                              key={`${skillIndex}-${skill}`}
                              className="tag max-w-full break-all bg-panel-2 text-ink-2"
                            >
                              {skill}
                            </span>
                          ))}
                    {position.highlights.map((highlight, highlightIndex) => (
                            <span
                              key={`${highlightIndex}-${highlight}`}
                              className="max-w-full break-words rounded-md bg-panel-2 px-2 py-0.5"
                            >
                              {highlight}
                            </span>
                          ))}
                  </div>
                )}

                {position.jd_text?.trim() && (
                  <details className="mt-2 rounded-lg bg-panel-2 px-3 py-2 text-xs">
                    <summary className="cursor-pointer font-medium text-ink-2">
                      {l(`查看 JD 原文（${position.jd_text.length} 字）`, `View original job description (${position.jd_text.length} characters)`) }
                    </summary>
                    <div className="mt-2 max-h-64 overflow-y-auto whitespace-pre-wrap break-words border-t border-line pt-2 leading-relaxed text-ink-2">
                      {position.jd_text}
                    </div>
                  </details>
                )}
              </div>
            </div>
          );
        })}
      </fieldset>

      <div className="flex flex-wrap items-center justify-between gap-3 border-t border-line-2 bg-panel-2/30 px-4 py-3">
        <span className="text-xs text-ink-3" aria-live="polite">
          {l(`已选择 ${includedCount} / ${operation.positions.length} 条变更`, `${includedCount} of ${operation.positions.length} changes selected`)}
        </span>
        <div className="flex items-center gap-2">
          <button
            type="button"
            onClick={onReject}
            disabled={actionsDisabled}
            className="btn btn-sm btn-danger"
          >
            {action === "reject" ? l("正在放弃…", "Discarding…") : l("全部放弃", "Discard all")}
          </button>
          <button
            type="button"
            onClick={onApprove}
            disabled={actionsDisabled || includedCount === 0}
            className="btn-primary btn-sm"
          >
            {action === "approve" ? l("正在保存…", "Saving…") : l(`保存 ${includedCount} 条`, `Save ${includedCount}`)}
          </button>
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

export function IntakeOperationsPanel({
  active,
  refreshSignal,
  className = "",
  operationIds,
  anchorIdForOperation,
  onOperationSettled,
}: {
  active: boolean;
  refreshSignal: number;
  className?: string;
  operationIds?: readonly string[];
  anchorIdForOperation?: OperationAnchorIdResolver;
  onOperationSettled?: (operationId: string) => void;
}) {
  const l = useLocalizer();
  const { locale } = useLocale();
  const normalizedOperationIds = operationIds === undefined
    ? null
    : [...new Set(operationIds)];
  const operationIdsKey = normalizedOperationIds?.join(",") ?? null;
  const [operations, setOperations] = useState<IntakeOperation[]>([]);
  const [excludedRows, setExcludedRows] = useState<Record<string, number[]>>({});
  const [action, setAction] = useState<{ operationId: string; action: IntakeAction } | null>(null);
  const [operationErrors, setOperationErrors] = useState<Record<string, string>>({});
  const [listError, setListError] = useState("");
  const [notice, setNotice] = useState<IntakeNotice | null>(null);
  const [loading, setLoading] = useState(true);
  const refreshEpochRef = useRef(0);
  const actionRef = useRef<string | null>(null); // Blocks same-frame duplicate clicks before state commits.
  const lifecycleAbortRef = useRef<AbortController | null>(null);
  const protectedOperationIdsRef = useRef<Set<string>>(new Set());
  const terminalOperationIdsRef = useRef<Set<string>>(new Set());
  const operationIdsRef = useRef<readonly string[] | null>(normalizedOperationIds);
  const onOperationSettledRef = useRef(onOperationSettled);
  const mountedRef = useRef(true);
  operationIdsRef.current = normalizedOperationIds;
  onOperationSettledRef.current = onOperationSettled;

  useEffect(() => {
    if (notice === null) return;
    const visibleNotice = notice;
    const timer = window.setTimeout(() => {
      setNotice((current) => current === visibleNotice ? null : current);
    }, 2000);
    return () => window.clearTimeout(timer);
  }, [notice]);

  const runRefreshBatch = useCallback(async (batch: OperationRefreshBatch) => {
    if (batch.showLoading) setLoading(true);
    try {
      const exactOperationIds = operationIdsRef.current;
      if (exactOperationIds !== null) {
        const requestedIds = new Set(exactOperationIds);
        protectedOperationIdsRef.current = new Set(
          [...protectedOperationIdsRef.current].filter((operationId) => (
            requestedIds.has(operationId)
          )),
        );
      }
      const exactReadResults = exactOperationIds === null
        ? null
        : await Promise.all(exactOperationIds.map(async (operationId) => {
          try {
            const canonical = await getIntakeOperation(operationId, {
              signal: lifecycleAbortRef.current?.signal,
            });
            if (canonical.operation_id !== operationId) {
              throw new Error(l("返回的操作记录与当前请求不一致，请重新核对。", "The returned operation does not match this request. Check again."));
            }
            return { operationId, canonical, error: null };
          } catch (error) {
            return { operationId, canonical: null, error };
          }
        }));
      const exactTerminalOperations = exactReadResults?.flatMap((result) => (
        result.canonical !== null && result.canonical.state !== "pending"
          ? [result.canonical]
          : []
      )) ?? [];
      const failedExactReads = exactReadResults?.filter(
        (result) => result.canonical === null,
      ) ?? [];
      const loaded = (exactReadResults === null
        ? await getPendingIntakeOperations({
            signal: lifecycleAbortRef.current?.signal,
          })
        : exactReadResults.flatMap((result) => (
            result.canonical?.state === "pending" ? [result.canonical] : []
          )))
        .filter((operation) => operation.state === "pending"
          && !terminalOperationIdsRef.current.has(operation.operation_id));
      if (!batch.isCurrent()) return;
      for (const operation of loaded) {
        protectedOperationIdsRef.current.delete(operation.operation_id);
      }
      for (const failed of failedExactReads) {
        protectedOperationIdsRef.current.add(failed.operationId);
      }
      // Freeze the protected set seen by this response because React may defer the updater.
      // This prevents an older list response from dropping card selections or errors after refs clear.
      const protectedOperationIds = new Set(protectedOperationIdsRef.current);
      setOperations((current) => mergePendingIntakeOperations(
        current,
        loaded,
        protectedOperationIds,
      ));
      setExcludedRows((current) => reconcileIntakeExcludedRows(
        current,
        loaded,
        protectedOperationIds,
      ));
      setOperationErrors((current) => retainIntakeOperationErrors(
        current,
        loaded,
        protectedOperationIds,
      ));
      if (loaded.length > 0 || protectedOperationIds.size > 0) {
        setNotice((current) => current?.preserveWithPending ? current : null);
      }
      for (const terminalOperation of exactTerminalOperations) {
        applyCanonicalOperation(terminalOperation);
      }
      const firstExactError = failedExactReads[0]?.error;
      setListError(firstExactError === undefined
        ? ""
        : firstExactError instanceof Error
          ? l(`待确认岗位刷新失败：${firstExactError.message}`, `Could not refresh pending roles: ${firstExactError.message}`)
          : l("待确认岗位刷新失败，请稍后重试。", "Could not refresh pending roles. Try again later."));
    } catch (reason) {
      if (!batch.isCurrent()) return;
      // Scheduled polling preserves the last trusted state; only manual or initial refresh reports errors.
      if (batch.reportError) {
        setListError(reason instanceof Error
          ? l(`待确认岗位刷新失败：${reason.message}`, `Could not refresh pending roles: ${reason.message}`)
          : l("待确认岗位刷新失败，请稍后重试。", "Could not refresh pending roles. Try again later."));
      }
    } finally {
      if (batch.showLoading && mountedRef.current) setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [l]);

  // Recover server-side work on mount. Each chat turn increments refreshSignal in finally;
  // an agent may emit a proposal after streaming stops, so visible chat polls serially at low frequency.
  const refreshPendingOperations = useOperationRefreshScheduler({
    active,
    refreshSignal,
    operationIdsKey,
    signalRefreshReportsError: false,
    refreshEpochRef,
    mountedRef,
    runBatch: runRefreshBatch,
  });

  useEffect(() => {
    const controller = new AbortController();
    lifecycleAbortRef.current = controller;
    return () => {
      controller.abort();
      if (lifecycleAbortRef.current === controller) lifecycleAbortRef.current = null;
    };
  }, []);

  function togglePosition(operationId: string, rowNumber: number, included: boolean) {
    if (actionRef.current !== null) return;
    setExcludedRows((current) => {
      const excluded = new Set(current[operationId] ?? []);
      if (included) excluded.delete(rowNumber);
      else excluded.add(rowNumber);
      return { ...current, [operationId]: [...excluded].sort((left, right) => left - right) };
    });
    setOperationErrors((current) => {
      // Only an authoritative result for the same ID can clear an uncertain terminal state.
      if (protectedOperationIdsRef.current.has(operationId)) return current;
      if (!(operationId in current)) return current;
      const next = { ...current };
      delete next[operationId];
      return next;
    });
  }

  function applyCanonicalOperation(
    operation: IntakeOperation,
    noticeOverride?: IntakeNotice,
  ): boolean {
    protectedOperationIdsRef.current.delete(operation.operation_id);
    // An earlier pending GET must not reinsert a card that has already completed.
    refreshEpochRef.current += 1;
    if (operation.state === "pending") {
      setOperations((current) => {
        const index = current.findIndex((item) => item.operation_id === operation.operation_id);
        if (index < 0) return [...current, operation];
        return current.map((item) => item.operation_id === operation.operation_id ? operation : item);
      });
      return false;
    }

    if (!terminalOperationIdsRef.current.has(operation.operation_id)) {
      terminalOperationIdsRef.current.add(operation.operation_id);
      try {
        onOperationSettledRef.current?.(operation.operation_id);
      } catch {
        // Persistence cleanup must not roll back a canonical terminal result.
      }
    }

    setOperations((current) => current.filter(
      (item) => item.operation_id !== operation.operation_id,
    ));
    setExcludedRows((current) => {
      const next = { ...current };
      delete next[operation.operation_id];
      return next;
    });
    setOperationErrors((current) => {
      const next = { ...current };
      delete next[operation.operation_id];
      return next;
    });

    if (noticeOverride) {
      setNotice({ ...noticeOverride, operationId: operation.operation_id });
    } else if (operation.state === "completed") {
      setNotice({
        kind: "status",
        message: completedOperationMessage(operation, locale),
        operationId: operation.operation_id,
      });
    } else if (operation.state === "rejected") {
      setNotice({
        kind: "status",
        message: l("这批岗位变更已取消，不会执行。", "These role changes were cancelled and will not be applied."),
        operationId: operation.operation_id,
      });
    } else {
      setNotice({
        kind: "alert",
        message: l("预览已过期或源记录已变化，本次变更未执行；请在对话中重新生成。", "The preview expired or source records changed, so nothing was applied. Generate it again in chat."),
        operationId: operation.operation_id,
      });
    }
    return true;
  }

  async function runAction(operation: IntakeOperation, nextAction: IntakeAction) {
    if (actionRef.current !== null || operation.state !== "pending") return;
    actionRef.current = operation.operation_id;
    protectedOperationIdsRef.current.add(operation.operation_id);
    const requestSignal = lifecycleAbortRef.current?.signal;
    setAction({ operationId: operation.operation_id, action: nextAction });
    setNotice(null);
    setOperationErrors((current) => {
      const next = { ...current };
      delete next[operation.operation_id];
      return next;
    });

    const requestedExcludedRows = normalizeExcludedRows(
      excludedRows[operation.operation_id] ?? [],
    );
    try {
      const canonical = nextAction === "approve"
        ? await approveIntakeOperation(
            operation.operation_id,
            requestedExcludedRows,
            { signal: requestSignal },
          )
        : await rejectIntakeOperation(operation.operation_id, { signal: requestSignal });
      if (!mountedRef.current) return;
      if (!applyCanonicalOperation(canonical)) {
      setOperationErrors((current) => ({
        ...current,
        [operation.operation_id]: nextAction === "approve"
            ? l("写入仍在处理中，可以稍后继续核对同一次选择。", "The write is still processing. You can check this same selection again later.")
            : l("放弃请求仍在处理中，可以稍后继续核对同一次选择。", "The discard request is still processing. You can check this same selection again later."),
        }));
      }
    } catch {
      if (!mountedRef.current) return;
      // If the write response is lost, reread the same operation instead of guessing its outcome.
      try {
        const canonical = await getIntakeOperation(operation.operation_id, {
          signal: requestSignal,
        });
        if (!mountedRef.current) return;
        const comparison = compareIntakeCommand(
          nextAction, requestedExcludedRows, canonical,
        );
        if (comparison === "different") {
          const mismatchMessage = canonical.state === "completed"
            ? nextAction === "approve"
              ? l(`另一窗口已按不同选择完成，本次选择未执行。${completedOperationMessage(canonical)}`, `Another window completed a different selection; this selection was not applied. ${completedOperationMessage(canonical, locale)}`)
              : l(`另一窗口已应用岗位变更，本次“全部不执行”未执行。${completedOperationMessage(canonical)}`, `Another window applied the role changes; this discard-all request was not applied. ${completedOperationMessage(canonical, locale)}`)
            : l("另一窗口已取消这批岗位变更，本次应用选择未执行。", "Another window cancelled these changes, so this apply selection was not executed.");
          applyCanonicalOperation(canonical, {
            kind: "alert",
            message: mismatchMessage,
            preserveWithPending: true,
          });
        } else if (!applyCanonicalOperation(canonical)) {
          setOperationErrors((current) => ({
            ...current,
            [operation.operation_id]: nextAction === "approve"
              ? l("应用请求没有完成，岗位变更尚未执行；可用同一操作重试。", "The apply request did not complete and no role changes were made. Retry the same operation.")
              : l("取消请求没有完成；可用同一操作重试。", "The cancellation did not complete. Retry the same operation."),
          }));
        }
      } catch (canonicalReason) {
        if (!mountedRef.current) return;
        const detail = canonicalReason instanceof Error ? l("，网络读取也暂时失败", ", and the network read also failed") : "";
        setOperationErrors((current) => ({
          ...current,
          [operation.operation_id]: l(`暂时无法确认最终结果${detail}。方案会保留；稍后重新核对只会继续确认同一次选择。`, `The final result cannot be confirmed${detail}. The proposal is preserved; checking later continues to verify the same selection.`),
        }));
      }
    } finally {
      if (actionRef.current === operation.operation_id) actionRef.current = null;
      if (mountedRef.current) {
        setAction((current) => current?.operationId === operation.operation_id ? null : current);
        void refreshPendingOperations(false);
      }
    }
  }

  if (!loading && !listError && !notice && operations.length === 0) return null;
  const anchoredContentIds = [
    ...operations.map((operation) => operation.operation_id),
    ...(notice?.operationId === undefined ? [] : [notice.operationId]),
  ];
  const allOperationContentAnchored = !loading && !listError
    && anchoredContentIds.length > 0
    && anchoredContentIds.every((operationId) => (
      operationAnchorElement(operationId, anchorIdForOperation) !== null
    ));
  return (
    <div
      role="region"
      aria-label={l("待确认岗位变更", "Pending role changes")}
      className={allOperationContentAnchored
        ? "contents"
        : `flex w-full flex-col gap-2.5 ${className}`.trim()}
    >
      {loading && operations.length === 0 && !listError && (
        <p role="status" className="text-xs text-ink-3">{l("正在核对待确认岗位…", "Checking pending roles…")}</p>
      )}
      {notice && renderOperationAtAnchor((
        <div
          role={notice.kind}
          className={`flex items-center justify-between gap-3 rounded-xl px-3 py-2 text-sm ${
            notice.kind === "alert" ? "bg-warn-soft text-warn" : "bg-ok-soft text-ok"
          }`}
        >
          <span className="min-w-0">{notice.message}</span>
        </div>
      ), notice.operationId ?? "", notice.operationId === undefined
        ? undefined
        : anchorIdForOperation, `intake-notice-${notice.operationId ?? "orphan"}`)}
      {listError && (
        <div role="alert" className="flex flex-wrap items-center justify-between gap-2 rounded-xl bg-bad-soft px-3 py-2 text-sm text-bad">
          <span>{listError}</span>
          <button
            type="button"
            onClick={() => void refreshPendingOperations(true, true)}
            disabled={loading}
            className="btn btn-sm"
          >
            {l("重新加载", "Reload")}
          </button>
        </div>
      )}
      {operations.map((operation) => renderOperationAtAnchor(
        <IntakeOperationCard
          key={operation.operation_id}
          operation={operation}
          excludedRowNumbers={excludedRows[operation.operation_id] ?? []}
          action={action?.operationId === operation.operation_id ? action.action : null}
          actionsDisabled={action !== null}
          error={operationErrors[operation.operation_id] ?? null}
          onToggle={(rowNumber, included) => togglePosition(
            operation.operation_id, rowNumber, included,
          )}
          onApprove={() => void runAction(operation, "approve")}
          onReject={() => void runAction(operation, "reject")}
        />,
        operation.operation_id,
        anchorIdForOperation,
        `intake-proposal-${operation.operation_id}`,
      ))}
    </div>
  );
}
