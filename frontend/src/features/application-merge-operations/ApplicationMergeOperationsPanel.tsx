import { useEffect, useMemo, useRef } from "react";

import { formatDate } from "../../i18n/formatters";
import type { UiLocale } from "../../i18n/i18n";
import { useLocale } from "../../i18n/localePreference";
import { useLocalizer } from "../../i18n/useLocalizer";

import {
  approveApplicationMergeOperation,
  getApplicationMergeOperation,
  getPendingApplicationMergeOperations,
  rejectApplicationMergeOperation,
} from "./applicationMergeOperationApi";
import type {
  ApplicationMergeCounts,
  ApplicationMergeFieldResolution,
  ApplicationMergeFinalDestination,
  ApplicationMergeOperation,
  ApplicationMergeResumeRef,
} from "./applicationMergeOperationContract";
import {
  useBinaryTrustedOperationQueue,
  type BinaryTrustedOperationAction,
  type BinaryTrustedOperationMessages,
  type BinaryTrustedOperationNoticePayload,
} from "../operations/useBinaryTrustedOperationQueue";
import {
  operationAnchorElement,
  renderOperationAtAnchor,
  type OperationAnchorIdResolver,
} from "../operations/operationAnchorPortal";

type ApplicationMergeAction = BinaryTrustedOperationAction;

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

const PREP_STATUS_LABELS: Record<string, string> = {
  none: "尚未生成",
  pending: "排队中",
  running: "生成中",
  ready: "已就绪",
  failed: "生成失败",
};

const OUTCOME_LABELS: Record<string, string> = {
  passed: "通过",
  failed: "未通过",
  cancelled: "取消",
};

const QUESTION_SOURCE_LABELS: Record<string, string> = {
  real: "真题",
  generated: "生成题",
  imported: "导入题",
};

const FIELD_LABELS: Record<ApplicationMergeFieldResolution["field"], string> = {
  company: "公司",
  position: "岗位",
  department: "部门",
  channel: "渠道",
  stage: "阶段",
  current_step: "当前环节",
  priority: "优先级",
  selected_resume: "当前选用简历",
  applied_date: "投递日期",
  next_action: "下一步安排",
  pause: "暂停信息",
  application_note: "岗位备注",
  jd: "JD 与结构化要求",
  prep: "岗位准备产物",
};

const STRATEGY_LABELS: Record<ApplicationMergeFieldResolution["strategy"], string> = {
  destination_identity: "采用保留记录身份",
  destination_preferred: "保留记录优先",
  source_fallback: "保留记录为空，采用源记录",
  highest_priority: "保留较高优先级",
  cleared_for_safety: "为避免旧产物错配而清空",
};

const STAGE_LABELS_EN: Record<string, string> = {
  backlog: "Considering", applied: "Applied", written_test: "Assessment",
  interviewing: "Interviewing", offer: "Offer", withdrawn: "Withdrawn",
  rejected: "Rejected", pooled: "On hold",
};
const PREP_STATUS_LABELS_EN: Record<string, string> = {
  none: "Not generated", pending: "Queued", running: "Generating",
  ready: "Ready", failed: "Generation failed",
};
const OUTCOME_LABELS_EN: Record<string, string> = {
  passed: "Passed", failed: "Not passed", cancelled: "Cancelled",
};
const QUESTION_SOURCE_LABELS_EN: Record<string, string> = {
  real: "Real", generated: "Generated", imported: "Imported",
};
const FIELD_LABELS_EN: Record<ApplicationMergeFieldResolution["field"], string> = {
  company: "Company", position: "Role", department: "Department", channel: "Channel",
  stage: "Stage", current_step: "Current step", priority: "Priority",
  selected_resume: "Selected résumé", applied_date: "Application date",
  next_action: "Next action", pause: "Pause details", application_note: "Role notes",
  jd: "Job description and requirements", prep: "Role-preparation artifacts",
};
const STRATEGY_LABELS_EN: Record<ApplicationMergeFieldResolution["strategy"], string> = {
  destination_identity: "Keep destination identity",
  destination_preferred: "Prefer destination value",
  source_fallback: "Use source when destination is empty",
  highest_priority: "Keep higher priority",
  cleared_for_safety: "Clear to prevent stale-artifact mismatch",
};

function formatOperationTime(value: string, locale: UiLocale = "zh-CN"): string {
  return formatDate(value, locale, "dateTime");
}

function operationIdentity(operation: ApplicationMergeOperation, locale: UiLocale = "zh-CN"): string {
  const source = operation.source;
  const destination = operation.destination;
  return locale === "en"
    ? `Source ${source.company} · ${source.position} → destination ${destination.company} · ${destination.position}`
    : `源岗位 ${source.company}·${source.position} → 保留岗位 ${destination.company}·${destination.position}`;
}

function movedCounts(operation: ApplicationMergeOperation): ApplicationMergeCounts {
  const effect = operation.effect;
  return {
    timeline_entries: effect.timeline_entries_rebound.length,
    questions: effect.questions_rebound.length,
    question_occurrences: effect.question_occurrences_rebound.length,
    resumes: effect.resumes_rebound.length,
  };
}

function addCounts(
  left: ApplicationMergeCounts,
  right: ApplicationMergeCounts,
): ApplicationMergeCounts {
  return {
    timeline_entries: left.timeline_entries + right.timeline_entries,
    questions: left.questions + right.questions,
    question_occurrences: left.question_occurrences + right.question_occurrences,
    resumes: left.resumes + right.resumes,
  };
}

function countsEqual(left: ApplicationMergeCounts, right: ApplicationMergeCounts): boolean {
  return left.timeline_entries === right.timeline_entries
    && left.questions === right.questions
    && left.question_occurrences === right.question_occurrences
    && left.resumes === right.resumes;
}

function resumeRefsEqual(
  left: ApplicationMergeResumeRef | null,
  right: ApplicationMergeResumeRef | null,
): boolean {
  if (left === null || right === null) return left === right;
  return left.id === right.id
    && left.name === right.name
    && left.binding === right.binding
    && left.application_id === right.application_id
    && left.archived === right.archived;
}

function stringArraysEqual(left: string[], right: string[]): boolean {
  return left.length === right.length && left.every((value, index) => value === right[index]);
}

function finalDestinationsEqual(
  left: ApplicationMergeFinalDestination,
  right: ApplicationMergeFinalDestination,
): boolean {
  return left.application_id === right.application_id
    && left.company === right.company
    && left.position === right.position
    && left.department === right.department
    && left.channel === right.channel
    && left.stage === right.stage
    && left.current_step === right.current_step
    && left.priority === right.priority
    && resumeRefsEqual(left.selected_resume, right.selected_resume)
    && left.applied_date === right.applied_date
    && JSON.stringify(left.next_action) === JSON.stringify(right.next_action)
    && left.paused_from_stage === right.paused_from_stage
    && left.pause_reason === right.pause_reason
    && left.application_note === right.application_note
    && left.jd_source === right.jd_source
    && left.jd_preview === right.jd_preview
    && left.jd_truncated === right.jd_truncated
    && stringArraysEqual(left.skills, right.skills)
    && stringArraysEqual(left.highlights, right.highlights)
    && left.prep_status === right.prep_status
    && left.prep_artifact_present === right.prep_artifact_present;
}

function completedResultMatchesPreview(operation: ApplicationMergeOperation): boolean {
  const result = operation.result;
  if (result === null) return false;
  const moved = movedCounts(operation);
  const totals = addCounts(operation.effect.destination_existing, moved);
  return result.status === "ok"
    && result.source_application_id === operation.source.application_id
    && result.destination_application_id === operation.destination.application_id
    && result.source_deleted
    && result.destination_prep_reset
    && countsEqual(result.moved, moved)
    && countsEqual(result.destination_totals, totals)
    && finalDestinationsEqual(result.final_destination, operation.effect.final_destination);
}

function completedMessage(operation: ApplicationMergeOperation, locale: UiLocale = "zh-CN"): string {
  const moved = operation.result?.moved ?? movedCounts(operation);
  if (locale === "en") return `${operationIdentity(operation, locale)} completed. The source was removed and the destination retained; moved ${moved.timeline_entries} history entries, ${moved.questions} questions, ${moved.question_occurrences} question occurrences, and ${moved.resumes} role-specific résumés.`;
  return `${operationIdentity(operation)} 已完成；源记录已移除，保留岗位继续沿用，`
    + `迁入 ${moved.timeline_entries} 条历程、${moved.questions} 道题、`
    + `${moved.question_occurrences} 条题目出处和 `
    + `${moved.resumes} 份岗位专属简历。`;
}

function terminalNotice(
  operation: ApplicationMergeOperation,
  locale: UiLocale = "zh-CN",
): BinaryTrustedOperationNoticePayload {
  const identity = operationIdentity(operation, locale);
  if (operation.state === "completed") {
    if (!completedResultMatchesPreview(operation)) {
      return {
        kind: "alert",
        message: locale === "en" ? `${identity} appears merged, but before-and-after verification disagrees. Refresh Application Tracker; the page will not report a contradictory result as success.` : `${identity} 显示已合并，但前后核对结果不一致；请刷新求职进展确认；页面不会将矛盾结果显示为成功。`,
      };
    }
    return { kind: "status", message: completedMessage(operation, locale) };
  }
  if (operation.state === "rejected") {
    return {
      kind: "status",
      message: locale === "en" ? `${identity} was not merged. This cancellation changed no role data; another operation may have updated a role or binding, so use the current records as authoritative.` : `${identity} 已取消合并；本次取消没有改动岗位数据。岗位或绑定可能已被其他操作更新，请以当前数据为准。`,
    };
  }
  return {
    kind: "alert",
    message: locale === "en" ? `${identity} is stale and can no longer be confirmed safely. Refresh and verify both roles in Application Tracker before starting again.` : `${identity} 的方案已过期，当前无法可靠确认是否合并；请先刷新并在求职进展中核对两条岗位，再决定是否重新发起。`,
  };
}

const APPLICATION_MERGE_API = {
  listPending: getPendingApplicationMergeOperations,
  get: getApplicationMergeOperation,
  approve: approveApplicationMergeOperation,
  reject: rejectApplicationMergeOperation,
};

function applicationMergeMessages(locale: UiLocale): BinaryTrustedOperationMessages<ApplicationMergeOperation> {
  const en = locale === "en";
  return {
  listError: () => en
    ? "Could not refresh pending role merges. Try again later."
    : "待确认岗位合并刷新失败，请稍后重试。",
  terminalNotice: (operation) => terminalNotice(operation, locale),
  oppositeCommandNotice: (operation, attempted) => {
    const identity = operationIdentity(operation, locale);
    if (operation.state === "completed" && attempted === "reject") {
      if (!completedResultMatchesPreview(operation)) {
        return {
          kind: "alert",
          message: en ? `Another window shows ${identity} as merged, but verification disagrees. This keep-both request was not applied; refresh Application Tracker.` : `另一窗口显示 ${identity} 已合并，但前后核对结果不一致；本次“保留两条”没有执行，请刷新求职进展确认。`,
        };
      }
      return {
        kind: "alert",
        message: en ? `Another window confirmed the merge; this keep-both request was not applied. ${completedMessage(operation, locale)}` : `另一窗口已确认合并；本次“保留两条”没有执行。${completedMessage(operation)}`,
      };
    }
    return {
      kind: "alert",
      message: en ? `Another window cancelled this merge. The cancellation changed no role data; use current records as authoritative. ${identity}.` : `另一窗口已取消这次合并；本次取消本身没有改动岗位数据。岗位或绑定可能已被其他操作更新，请以当前数据为准。${identity}。`,
    };
  },
  pendingAfterResponse: (command) => command === "approve"
    ? en ? "The merge is still processing. You can check the same choice again later." : "合并仍在处理中，可以稍后继续核对同一次选择。"
    : en ? "The keep request is still processing. You can check the same choice again later." : "保留请求仍在处理中，可以稍后继续核对同一次选择。",
  pendingAfterRecovery: (command) => command === "approve"
    ? en ? "The merge did not complete and both roles remain. Retry the same operation." : "合并请求没有完成，两条岗位尚未合并；可用同一操作重试。"
    : en ? "The keep request did not complete. Retry the same operation." : "保留请求没有完成；可用同一操作重试。",
  mismatchedOperation: () => en
    ? "The returned operation does not match this request. Check it again."
    : "返回的操作记录与当前请求不一致，请重新核对。",
  missingFromPending: () => en
    ? "This operation left the pending list, but its final state cannot be verified. The card remains to prevent a false result; check it again."
    : "该操作已不在待办列表，但最终状态暂时无法核对。为避免误判，卡片已保留；请重新核对。",
  unknownAfterSubmit: (_submitReason, _readReason) => {
    return en ? "The network was interrupted, so the merge cannot be confirmed. The proposal is preserved; rechecking verifies the same choice." : "网络中断，暂时无法确认是否合并。方案会保留；重新核对只会继续确认同一次选择。";
  },
  unknownAfterRecheck: () => en
    ? "The final state still cannot be verified. The card remains so you can check again later."
    : "最终状态仍无法核对。卡片继续保留；稍后可再次核对。",
  };
}

function displayResolutionValue(
  resolution: ApplicationMergeFieldResolution,
  value: string | null,
  locale: UiLocale = "zh-CN",
): string {
  if (value === null || value === "") return locale === "en" ? "Not set" : "未填写";
  if (resolution.field === "stage") return (locale === "en" ? STAGE_LABELS_EN : STAGE_LABELS)[value] ?? value;
  if (resolution.field === "prep") {
    const [status, ...rest] = value.split("；");
    return [(locale === "en" ? PREP_STATUS_LABELS_EN : PREP_STATUS_LABELS)[status] ?? status, ...rest].join(locale === "en" ? "; " : "；");
  }
  return value;
}

function ResolutionValue({
  resolution,
  value,
  label,
}: {
  resolution: ApplicationMergeFieldResolution;
  value: string | null;
  label: string;
}) {
  const l = useLocalizer();
  const { locale } = useLocale();
  const displayed = resolution.field === "jd" && value
    ? l("已保存；完整摘要预览、技能和亮点见下方详细信息", "Saved; see the complete summary, skills, and highlights below")
    : displayResolutionValue(resolution, value, locale);
  const needsOwnScroll = displayed.includes("\n") || displayed.length > 180;
  return (
    <div
      className="max-h-28 min-w-36 overflow-y-auto whitespace-pre-wrap break-words"
      tabIndex={needsOwnScroll ? 0 : undefined}
      aria-label={label}
    >
      {displayed}
    </div>
  );
}

type StructuredRequirementsSnapshot = Pick<
  ApplicationMergeOperation["source"],
  "jd_preview" | "jd_truncated" | "skills" | "highlights"
>;

function RequirementValues({
  label,
  values,
  finalValues,
  final,
}: {
  label: string;
  values: string[];
  finalValues: string[];
  final: boolean;
}) {
  const l = useLocalizer();
  if (values.length === 0) {
    return (
      <div>
        <dt className="text-ink-3">{label}</dt>
        <dd className="mt-0.5 text-ink-3">{l("未填写", "Not set")}</dd>
      </div>
    );
  }
  const displayedValues = final
    ? values
    : [
        ...values.filter((value) => !finalValues.includes(value)),
        ...values.filter((value) => finalValues.includes(value)),
      ];
  const needsOwnScroll = values.length > 8
    || values.reduce((total, value) => total + value.length, 0) > 300;
  return (
    <div>
      <dt className="text-ink-3">{label}（{values.length}）</dt>
      <dd>
        <ul
          className="mt-1 max-h-36 space-y-1 overflow-y-auto pr-1"
          tabIndex={needsOwnScroll ? 0 : undefined}
          aria-label={l(`${label}完整列表`, `Complete ${label} list`)}
        >
          {displayedValues.map((value, index) => {
            const retained = final || finalValues.includes(value);
            return (
              <li
                key={`${index}:${value}`}
                className={retained
                  ? "break-words rounded-md bg-panel/70 px-2 py-1 text-ink-2"
                  : "break-words rounded-md bg-warn-soft px-2 py-1 text-warn"}
              >
                {value}
                {!final && (
                  <span className="ml-1 whitespace-nowrap text-[11px]">
                    {retained ? l("· 合并后仍存在", "· retained after merge") : l("· 不会带入", "· not carried forward")}
                  </span>
                )}
              </li>
            );
          })}
        </ul>
      </dd>
    </div>
  );
}

function StructuredRequirementsCard({
  kind,
  snapshot,
  finalDestination,
}: {
  kind: "source" | "destination" | "final";
  snapshot: StructuredRequirementsSnapshot;
  finalDestination: ApplicationMergeFinalDestination;
}) {
  const l = useLocalizer();
  const { locale } = useLocale();
  const final = kind === "final";
  const jdSelected = final || finalDestination.jd_source === kind;
  const labels = {
    source: l("源记录 · JD 与结构化要求", "Source · job description and requirements"),
    destination: l("保留记录 · JD 与结构化要求", "Destination · job description and requirements"),
    final: l("合并后 · JD 与结构化要求", "After merge · job description and requirements"),
  } as const;
  const sourceLabels = {
    source: l("源记录", "Source"),
    destination: l("保留记录", "Destination"),
    none: l("无", "None"),
  } as const;
  const hasJd = Boolean(
    snapshot.jd_preview || snapshot.skills.length > 0 || snapshot.highlights.length > 0,
  );
  return (
    <article className={kind === "source"
      ? "rounded-lg border border-bad/25 bg-bad-soft/50 px-3 py-2.5"
      : kind === "final"
        ? "rounded-lg border border-ok/25 bg-ok-soft/50 px-3 py-2.5"
        : "rounded-lg border border-line bg-panel-2 px-3 py-2.5"}
    >
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h4 className="text-xs font-medium text-ink-2">{labels[kind]}</h4>
        {final ? (
          <span className="text-[11px] text-ok">
            {l("最终 JD 来源：", "Final job-description source: ")}{sourceLabels[finalDestination.jd_source]}
          </span>
        ) : hasJd ? (
          <span className={jdSelected ? "text-[11px] text-ok" : "text-[11px] text-warn"}>
            {jdSelected ? l("此记录的 JD 会带入", "This job description is carried forward") : l("此记录的 JD 不会带入", "This job description is not carried forward")}
          </span>
        ) : null}
      </div>

      <dl className="mt-2 space-y-2 text-xs">
        <div>
          <dt className="text-ink-3">{l("JD 摘要预览", "Job-description summary")}</dt>
          <dd
            className="mt-1 max-h-40 overflow-y-auto whitespace-pre-wrap break-words rounded-md bg-panel/70 px-2 py-1.5 text-ink-2"
            tabIndex={snapshot.jd_preview.length > 320 ? 0 : undefined}
            aria-label={l(`${labels[kind]}的 JD 摘要预览`, `${labels[kind]} summary preview`)}
          >
            {snapshot.jd_preview || l("未保存", "Not saved")}
          </dd>
          {snapshot.jd_truncated && (
            <p className="mt-1 font-medium text-warn">
              {l("原始 JD 较长；这里仅显示确认所需的摘要预览，未展示部分不会被当作完整原文。", "The original job description is long; this is only the summary needed for confirmation, and omitted text is not treated as the complete source.")}
            </p>
          )}
        </div>
        <RequirementValues
          label={l("技能", "Skills")}
          values={snapshot.skills}
          finalValues={finalDestination.skills}
          final={final}
        />
        <RequirementValues
          label={l("亮点", "Highlights")}
          values={snapshot.highlights}
          finalValues={finalDestination.highlights}
          final={final}
        />
      </dl>
    </article>
  );
}

function SnapshotCard({
  kind,
  application,
}: {
  kind: "source" | "destination";
  application: ApplicationMergeOperation["source"];
}) {
  const l = useLocalizer();
  const { locale } = useLocale();
  const stages = locale === "en" ? STAGE_LABELS_EN : STAGE_LABELS;
  const prepStatuses = locale === "en" ? PREP_STATUS_LABELS_EN : PREP_STATUS_LABELS;
  const source = kind === "source";
  return (
    <article className={source
      ? "rounded-xl border border-bad/30 bg-bad-soft px-3 py-2.5"
      : "rounded-xl border border-ok/30 bg-ok-soft px-3 py-2.5"}
    >
      <p className={source ? "text-xs font-semibold text-bad" : "text-xs font-semibold text-ok"}>
        {source ? l("源记录 · 合并后移除", "Source · removed after merge") : l("保留记录 · ID 保持不变", "Destination · ID remains unchanged")}
      </p>
      <p className="mt-1 break-words text-sm font-medium text-ink">
        #{application.application_id} · {application.company} · {application.position}
      </p>
      <dl className="mt-2 grid gap-x-3 gap-y-1 text-xs sm:grid-cols-2">
        <div>
          <dt className="inline text-ink-3">{l("阶段：", "Stage: ")}</dt>
          <dd className="inline text-ink-2">{stages[application.stage] ?? application.stage}</dd>
        </div>
        <div>
          <dt className="inline text-ink-3">{l("当前环节：", "Current step: ")}</dt>
          <dd className="inline text-ink-2">{application.current_step || l("未填写", "Not set")}</dd>
        </div>
        <div>
          <dt className="inline text-ink-3">{l("准备产物：", "Preparation artifacts: ")}</dt>
          <dd className="inline text-ink-2">
            {prepStatuses[application.prep_status] ?? application.prep_status}
            {application.prep_artifact_present ? l(" · 已保存", " · saved") : l(" · 无产物", " · none")}
          </dd>
        </div>
        <div>
          <dt className="inline text-ink-3">{l("确认时版本：", "Version at confirmation: ")}</dt>
          <dd className="inline tabular-nums text-ink-2">
            {formatOperationTime(application.application_updated_time, locale)
              || application.application_updated_time}
          </dd>
        </div>
      </dl>
    </article>
  );
}

function FieldResolutionTable({
  resolutions,
}: {
  resolutions: ApplicationMergeFieldResolution[];
}) {
  const l = useLocalizer();
  const { locale } = useLocale();
  const fieldLabels = locale === "en" ? FIELD_LABELS_EN : FIELD_LABELS;
  const strategyLabels = locale === "en" ? STRATEGY_LABELS_EN : STRATEGY_LABELS;
  const discarded = resolutions.filter(
    (resolution) => resolution.source_value !== null
      && !resolution.source_value_carried_forward,
  );
  return (
    <div>
      <div className="flex flex-wrap items-baseline justify-between gap-2">
        <h3 className="text-xs font-medium text-ink-2">{l("12 项字段存活决议", "12 field-resolution decisions")}</h3>
        <span className={discarded.length > 0 ? "text-xs text-warn" : "text-xs text-ok"}>
          {discarded.length > 0
            ? l(`${discarded.length} 项源值不会带入，已逐项标出`, `${discarded.length} source values are not carried forward and are marked below`)
            : l("源记录中的已填写值均得到保留或重算", "Every populated source value is retained or recalculated")}
        </span>
      </div>
      <div
        role="region"
        aria-label={l("字段存活决议表，可横向滚动", "Field-resolution table; scroll horizontally")}
        tabIndex={0}
        className="mt-1 overflow-x-auto rounded-lg border border-line focus:outline-none focus:ring-2 focus:ring-accent/40"
      >
        <table className="w-full min-w-[48rem] table-fixed text-left text-xs">
          <thead className="bg-panel-2 text-ink-3">
            <tr>
              <th scope="col" className="w-28 px-3 py-2 font-medium">{l("字段 / 规则", "Field / rule")}</th>
              <th scope="col" className="px-3 py-2 font-medium">{l("源记录", "Source")}</th>
              <th scope="col" className="px-3 py-2 font-medium">{l("保留记录", "Destination")}</th>
              <th scope="col" className="px-3 py-2 font-medium">{l("合并后", "After merge")}</th>
            </tr>
          </thead>
          <tbody>
            {resolutions.map((resolution) => {
              const discardedSource = resolution.source_value !== null
                && !resolution.source_value_carried_forward;
              return (
                <tr
                  key={resolution.field}
                  className={discardedSource ? "border-t border-line bg-warn-soft/60" : "border-t border-line"}
                >
                  <th scope="row" className="align-top px-3 py-2 font-medium text-ink-2">
                    <span className="block">{fieldLabels[resolution.field]}</span>
                    <span className="mt-0.5 block font-normal text-ink-3">
                      {strategyLabels[resolution.strategy]}
                    </span>
                    <span className={discardedSource
                      ? "mt-1 block font-normal text-warn"
                      : "mt-1 block font-normal text-ok"}
                    >
                      {discardedSource ? l("源值不会带入", "Source value not carried forward") : l("源值已保留或无需带入", "Source value retained or not needed")}
                    </span>
                  </th>
                  <td className="align-top px-3 py-2 text-ink-2">
                    <ResolutionValue
                      resolution={resolution}
                      value={resolution.source_value}
                      label={l(`${FIELD_LABELS[resolution.field]}的源记录值`, `Source value for ${FIELD_LABELS_EN[resolution.field]}`)}
                    />
                  </td>
                  <td className="align-top px-3 py-2 text-ink-2">
                    <ResolutionValue
                      resolution={resolution}
                      value={resolution.destination_value}
                      label={l(`${FIELD_LABELS[resolution.field]}的保留记录值`, `Destination value for ${FIELD_LABELS_EN[resolution.field]}`)}
                    />
                  </td>
                  <td className="align-top px-3 py-2 font-medium text-ink">
                    <ResolutionValue
                      resolution={resolution}
                      value={resolution.final_value}
                      label={l(`${FIELD_LABELS[resolution.field]}的合并后值`, `Final value for ${FIELD_LABELS_EN[resolution.field]}`)}
                    />
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function ApplicationMergeOperationCard({
  operation,
  action,
  actionsDisabled,
  uncertain,
  error,
  onApprove,
  onReject,
  onRecheck,
}: {
  operation: ApplicationMergeOperation;
  action: ApplicationMergeAction | null;
  actionsDisabled: boolean;
  uncertain: boolean;
  error: string | null;
  onApprove: () => void;
  onReject: () => void;
  onRecheck: () => void;
}) {
  const l = useLocalizer();
  const { locale } = useLocale();
  const stages = locale === "en" ? STAGE_LABELS_EN : STAGE_LABELS;
  const outcomes = locale === "en" ? OUTCOME_LABELS_EN : OUTCOME_LABELS;
  const questionSources = locale === "en" ? QUESTION_SOURCE_LABELS_EN : QUESTION_SOURCE_LABELS;
  const { source, destination, effect } = operation;
  const createdLabel = formatOperationTime(operation.created_time, locale);
  const moved = movedCounts(operation);
  const destinationTotals = addCounts(effect.destination_existing, moved);

  return (
    <section
      id={`application-merge-operation-${operation.operation_id}`}
      aria-labelledby={`application-merge-operation-title-${operation.operation_id}`}
      className="card scroll-mt-20 overflow-hidden border-warn/35"
    >
      <div className="flex flex-wrap items-start justify-between gap-2 border-b border-line bg-warn-soft px-4 py-3">
        <div className="min-w-0">
          <div className="flex items-center gap-2">
            <span className="h-2 w-2 shrink-0 rounded-full bg-warn" aria-hidden />
            <h2
              id={`application-merge-operation-title-${operation.operation_id}`}
              className="break-words text-sm font-semibold"
            >
              {l("准备合并岗位", "Prepare to merge roles")} · {source.company}·{source.position} → {destination.company}·{destination.position}
            </h2>
          </div>
          <p className="mt-1 text-xs leading-relaxed text-warn">
            {l("勾选后把源岗位并入保留岗位并移除源记录。下方已展开字段取值与关联迁移范围。", "Confirm to merge the source into the destination and remove the source record. Field choices and linked-data migration are expanded below.")}
          </p>
        </div>
        {createdLabel && (
          <span className="shrink-0 text-xs tabular-nums text-ink-3">{createdLabel}</span>
        )}
      </div>

      <details open className="border-b border-line">
        <summary className="cursor-pointer px-4 py-2.5 text-xs font-medium text-ink-2">
          {l("查看合并字段与迁移范围", "View merge fields and migration scope")}
        </summary>
        <div
          className="max-h-[min(68vh,40rem)] space-y-4 overflow-y-auto border-t border-line px-4 py-3"
          tabIndex={0}
          aria-label={l(`${source.company}·${source.position} 合并到 ${destination.company}·${destination.position} 的影响明细`, `Merge-impact details for ${source.company} · ${source.position} into ${destination.company} · ${destination.position}`)}
        >
        <div className="grid gap-2 sm:grid-cols-2">
          <SnapshotCard kind="source" application={source} />
          <SnapshotCard kind="destination" application={destination} />
        </div>

        <FieldResolutionTable resolutions={effect.field_resolutions} />

        <div>
          <div className="flex flex-wrap items-baseline justify-between gap-2">
            <h3 className="text-xs font-medium text-ink-2">{l("JD 与结构化要求完整存活明细", "Complete job-description and requirements resolution")}</h3>
            <span className="text-xs text-ink-3">
              {l("源记录独有且不会带入的技能或亮点已逐项标黄", "Source-only skills or highlights that will not carry forward are marked")}
            </span>
          </div>
          <div className="mt-1 grid gap-2 lg:grid-cols-3">
            <StructuredRequirementsCard
              kind="source"
              snapshot={source}
              finalDestination={effect.final_destination}
            />
            <StructuredRequirementsCard
              kind="destination"
              snapshot={destination}
              finalDestination={effect.final_destination}
            />
            <StructuredRequirementsCard
              kind="final"
              snapshot={effect.final_destination}
              finalDestination={effect.final_destination}
            />
          </div>
        </div>

        <div className="rounded-lg bg-info-soft px-3 py-2 text-xs leading-relaxed text-info">
          <p className="font-medium">{l(`精确引用迁移：稳定 ID 不变，只改绑到保留岗位 #${destination.application_id}`, `Exact reference migration: stable IDs stay unchanged and are rebound to destination #${destination.application_id}`)}</p>
          <dl className="mt-2 grid gap-2 sm:grid-cols-4">
            {([
              [l("历程", "History"), "timeline_entries", moved.timeline_entries, destinationTotals.timeline_entries],
              [l("题目", "Questions"), "questions", moved.questions, destinationTotals.questions],
              [l("题目出处", "Question occurrences"), "question_occurrences", moved.question_occurrences, destinationTotals.question_occurrences],
              [l("岗位专属简历", "Role-specific résumés"), "resumes", moved.resumes, destinationTotals.resumes],
            ] as const).map(([label, key, movedValue, total]) => (
              <div key={key} className="rounded-lg bg-panel/70 px-2 py-1.5">
                <dt className="text-info/80">{label}</dt>
                <dd className="mt-0.5 tabular-nums text-ink-2">
                  {l(`迁入 ${movedValue} · 合并后 ${total}`, `Moved ${movedValue} · ${total} after merge`)}
                </dd>
                <dd className="tabular-nums text-ink-3">
                  {l(`原有 ${effect.destination_existing[key]}`, `${effect.destination_existing[key]} existing`)}
                </dd>
              </div>
            ))}
          </dl>
        </div>

        {effect.timeline_entries_rebound.length > 0 && (
          <details className="rounded-lg bg-panel-2 px-3 py-2 text-xs">
            <summary className="cursor-pointer font-medium text-ink-2">
              {l(`查看迁入的 ${effect.timeline_entries_rebound.length} 条历程`, `View ${effect.timeline_entries_rebound.length} moved history entries`)}
            </summary>
            <ul className="mt-2 space-y-1.5">
              {effect.timeline_entries_rebound.map((entry) => (
                <li key={entry.id} className="rounded-lg bg-panel px-3 py-2 text-ink-2">
                  <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                    <span className="font-medium">
                      #{entry.id} · {entry.step || entry.to_step || l("历程记录", "History entry")}
                    </span>
                    {entry.outcome && <span>{outcomes[entry.outcome] ?? entry.outcome}</span>}
                    {entry.occurred_date && <span className="tabular-nums text-ink-3">{entry.occurred_date}</span>}
                    {entry.journal_id !== null && <span className="text-ink-3">{l(`复盘 #${entry.journal_id}`, `Review #${entry.journal_id}`)}</span>}
                  </div>
                  {entry.from_stage !== entry.to_stage && (
                    <p className="mt-1 text-ink-3">
                      {stages[entry.from_stage] ?? entry.from_stage} → {stages[entry.to_stage] ?? entry.to_stage}
                    </p>
                  )}
                  {entry.summary && (
                    <p className="mt-1 max-h-20 overflow-y-auto whitespace-pre-wrap break-words text-ink-3">
                      {entry.summary}
                    </p>
                  )}
                </li>
              ))}
            </ul>
          </details>
        )}

        {effect.questions_rebound.length > 0 && (
          <details className="rounded-lg bg-panel-2 px-3 py-2 text-xs">
            <summary className="cursor-pointer font-medium text-ink-2">
              {l(`查看迁入的 ${effect.questions_rebound.length} 道题`, `View ${effect.questions_rebound.length} moved questions`)}
            </summary>
            <ul className="mt-2 list-disc space-y-1.5 pl-4 text-ink-2">
              {effect.questions_rebound.map((question) => (
                <li key={question.id} className="break-words">
                  #{question.id} · {questionSources[question.source] ?? question.source} · {question.text_preview}
                  {question.text_truncated ? l("（题面预览已截断）", " (question preview truncated)") : ""}
                  <span className="block text-ink-3">
                    {l("公司出处：", "Company source: ")}{question.company_before || l("未填写", "Not set")} → {question.company_after}
                    {question.journal_id !== null ? l(` · 复盘 #${question.journal_id}`, ` · Review #${question.journal_id}`) : ""}
                  </span>
                </li>
              ))}
            </ul>
          </details>
        )}

        {effect.question_occurrences_rebound.length > 0 && (
          <details className="rounded-lg bg-panel-2 px-3 py-2 text-xs">
            <summary className="cursor-pointer font-medium text-ink-2">
              {l(`查看迁入的 ${effect.question_occurrences_rebound.length} 条复盘题目出处`, `View ${effect.question_occurrences_rebound.length} moved review-question occurrences`)}
            </summary>
            <ul className="mt-2 list-disc space-y-1 pl-4 text-ink-2">
              {effect.question_occurrences_rebound.map((occurrence) => (
                <li key={`${occurrence.journal_id}:${occurrence.question_id}`}>
                  {l(`复盘 #${occurrence.journal_id} · 题目 #${occurrence.question_id} · 公司出处：`, `Review #${occurrence.journal_id} · Question #${occurrence.question_id} · Company source: `)}
                  {occurrence.company_before} → {occurrence.company_after}
                </li>
              ))}
            </ul>
          </details>
        )}

        {effect.resumes_rebound.length > 0 && (
          <details className="rounded-lg bg-panel-2 px-3 py-2 text-xs">
            <summary className="cursor-pointer font-medium text-ink-2">
              {l(`查看迁入的 ${effect.resumes_rebound.length} 份岗位专属简历`, `View ${effect.resumes_rebound.length} moved role-specific résumés`)}
            </summary>
            <ul className="mt-2 list-disc space-y-1 pl-4 text-ink-2">
              {effect.resumes_rebound.map((resume) => (
                <li key={resume.id} className="break-words">
                  #{resume.id} · {resume.name} · {l("岗位专属", "Role-specific")} · {resume.archived ? l("已归档", "Archived") : l("未归档", "Active")}
                </li>
              ))}
            </ul>
            <p className="mt-1 text-ink-3">{l("简历本体、岗位专属范围和归档状态均不变。", "Résumé content, role-specific scope, and archive state remain unchanged.")}</p>
          </details>
        )}

        <div className="rounded-lg bg-warn-soft px-3 py-2 text-xs leading-relaxed text-warn">
          <p className="font-medium">{l("准备产物会安全失效", "Preparation artifacts are invalidated safely")}</p>
          <ul className="mt-1 list-disc space-y-1 pl-4">
            <li>{l("源岗位随记录移除，其已保存的调研、匹配等准备产物一并移除。", "Saved research, matching, and other preparation artifacts are removed with the source role.")}</li>
            <li>{l("保留岗位的准备状态重置为“尚未生成”，旧产物不会冒充合并后的结果。", "The destination preparation state resets to Not generated, so stale artifacts cannot appear as post-merge results.")}</li>
            {(source.prep_status === "pending" || source.prep_status === "running"
              || destination.prep_status === "pending" || destination.prep_status === "running") && (
              <li>{l("已经发出的模型调用未必能立即取消，但旧任务不会覆盖合并后的确认结果。", "Model calls already sent may not stop immediately, but stale tasks cannot overwrite the confirmed merge result.")}</li>
            )}
          </ul>
        </div>

        <aside className="rounded-lg bg-info-soft px-3 py-2 text-xs leading-relaxed text-info">
          <p className="font-medium">{l("这次合并不会触碰的其他数据", "Other data this merge does not affect")}</p>
          <ul className="mt-1 list-disc space-y-1 pl-4">
            {effect.company_records_untouched && <li>{l("公司档案、公司调研与公司备注不会合并或删除", "Company profiles, research, and notes are not merged or deleted")}</li>}
            {effect.journal_records_untouched && <li>{l("复盘、编辑和导入原文及其记录号保持不变", "Reviews, edits, imported source text, and their IDs remain unchanged")}</li>}
            {effect.external_logs_untouched && <li>{l("外部日志保持不变", "External logs remain unchanged")}</li>}
          </ul>
        </aside>
        </div>
      </details>

      <div className="flex flex-wrap items-center justify-end gap-2 border-t border-line bg-panel-2/40 px-4 py-3">
        {uncertain && (
          <button
            type="button"
            onClick={onRecheck}
            disabled={actionsDisabled}
            className="btn btn-sm"
          >
            {action === "recheck" ? l("正在核对…", "Checking…") : l("重新核对状态", "Recheck status")}
          </button>
        )}
        <button
          type="button"
          onClick={onReject}
          disabled={actionsDisabled || uncertain}
          className="btn btn-sm"
        >
          {action === "reject" ? l("正在保留…", "Keeping…") : l("✕ 保留两条岗位", "✕ Keep both roles")}
        </button>
        <button
          type="button"
          onClick={onApprove}
          disabled={actionsDisabled || uncertain}
          className="btn btn-sm border-warn/40 text-warn hover:bg-warn-soft"
        >
          {action === "approve" ? l("正在合并…", "Merging…") : l("✓ 合并岗位", "✓ Merge roles")}
        </button>
      </div>
      {error && (
        <p role="alert" className="border-t border-line bg-bad-soft px-4 py-2.5 text-xs text-bad">
          {error}
        </p>
      )}
    </section>
  );
}

export function ApplicationMergeOperationsPanel({
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
  const messages = useMemo(() => applicationMergeMessages(locale), [locale]);
  const queue = useBinaryTrustedOperationQueue<ApplicationMergeOperation>({
    active,
    refreshSignal,
    api: APPLICATION_MERGE_API,
    messages,
    operationIds,
    onOperationSettled: (operation) => onOperationSettled?.(operation.operation_id),
  });
  const announcedOperationIdsRef = useRef(new Set<string>());

  useEffect(() => {
    if (!active) return;
    let firstNewOperationId: string | null = null;
    for (const operation of queue.operations) {
      if (announcedOperationIdsRef.current.has(operation.operation_id)) continue;
      announcedOperationIdsRef.current.add(operation.operation_id);
      firstNewOperationId ??= operation.operation_id;
    }
    if (firstNewOperationId !== null) onOperationAppeared?.(firstNewOperationId);
  }, [active, onOperationAppeared, queue.operations]);

  if (!queue.loading && !queue.listError
      && queue.notices.length === 0 && queue.operations.length === 0) return null;
  const operationContentIds = [
    ...queue.notices.map((notice) => notice.operationId),
    ...queue.operations.map((operation) => operation.operation_id),
  ];
  const allOperationContentAnchored = !queue.loading && !queue.listError
    && operationContentIds.length > 0
    && operationContentIds.every((operationId) => (
      operationAnchorElement(operationId, anchorIdForOperation) !== null
    ));
  return (
    <div
      role="region"
      aria-label={l("待确认岗位合并", "Role merges awaiting confirmation")}
      className={allOperationContentAnchored
        ? "contents"
        : ["flex w-full flex-col gap-2.5", className].filter(Boolean).join(" ")}
    >
      <span className="sr-only" aria-live="polite">
        {queue.operations.length > 0 ? l(`有 ${queue.operations.length} 条岗位合并等待确认`, `${queue.operations.length} role merges await confirmation`) : ""}
      </span>
      {queue.loading && queue.operations.length === 0 && !queue.listError && (
        <p role="status" className="text-xs text-ink-3">{l("正在核对待确认岗位合并…", "Checking pending role merges…")}</p>
      )}
      {queue.notices.map((notice) => renderOperationAtAnchor((
        <div
          key={notice.operationId}
          role={notice.kind}
          className={notice.kind === "alert"
            ? "rounded-xl bg-warn-soft px-3 py-2 text-sm text-warn"
            : "rounded-xl bg-ok-soft px-3 py-2 text-sm text-ok"}
        >
          <span className="min-w-0 break-words">{notice.message}</span>
        </div>
      ), notice.operationId, anchorIdForOperation,
      `application-merge-notice-${notice.operationId}`))}
      {queue.listError && (
        <div role="alert" className="flex flex-wrap items-center justify-between gap-2 rounded-xl bg-bad-soft px-3 py-2 text-sm text-bad">
          <span>{queue.listError}</span>
          <button
            type="button"
            onClick={() => void queue.refreshPendingOperations(true, true)}
            disabled={queue.loading}
            className="btn btn-sm"
          >
            {l("重新加载", "Reload")}
          </button>
        </div>
      )}
      {queue.operations.map((operation) => renderOperationAtAnchor((
        <ApplicationMergeOperationCard
          key={operation.operation_id}
          operation={operation}
          action={queue.action?.operationId === operation.operation_id
            ? queue.action.action
            : null}
          actionsDisabled={queue.action !== null}
          uncertain={queue.uncertainOperationIds.has(operation.operation_id)}
          error={queue.operationErrors[operation.operation_id] ?? null}
          onApprove={() => void queue.runAction(operation, "approve")}
          onReject={() => void queue.runAction(operation, "reject")}
          onRecheck={() => void queue.recheckOperation(operation)}
        />
      ), operation.operation_id, anchorIdForOperation,
      `application-merge-proposal-${operation.operation_id}`))}
    </div>
  );
}
