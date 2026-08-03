import type { ApplicationPriority } from "../applications/applicationContract";
import type {
  ApplicationStage,
  BoardItem,
  TimelineEntry,
} from "./timelineContract";
import type { UiLocale } from "../../i18n/i18n";

export const PRIORITY_META: Record<Exclude<ApplicationPriority, null>, {
  label: string;
  badgeClass: string;
  dotClass: string;
}> = {
  high: { label: "高", badgeClass: "bg-bad-soft text-bad", dotClass: "bg-bad" },
  medium: { label: "中", badgeClass: "bg-warn-soft text-warn", dotClass: "bg-warn" },
  low: { label: "低", badgeClass: "bg-info-soft text-info", dotClass: "bg-info" },
};

const PRIORITY_ORDER: Record<Exclude<ApplicationPriority, null> | "none", number> = {
  high: 0,
  medium: 1,
  low: 2,
  none: 3,
};

export function sortByPriorityAndCreatedTime(rows: BoardItem[]): BoardItem[] {
  return [...rows].sort((a, b) => {
    const byPriority = PRIORITY_ORDER[a.priority ?? "none"] - PRIORITY_ORDER[b.priority ?? "none"];
    if (byPriority !== 0) return byPriority;
    const byCreatedTime = b.created_time.localeCompare(a.created_time);
    return byCreatedTime !== 0 ? byCreatedTime : b.id - a.id;
  });
}

// Timeline presentation reads future data only from next_action and history only from facts.
export const COLUMNS: readonly (readonly [ApplicationStage, string])[] = [
  ["backlog", "待定"],
  ["applied", "已投递"],
  ["written_test", "笔试中"],
  ["interviewing", "面试中"],
  ["offer", "Offer"],
  ["pooled", "泡池子"],
  ["withdrawn", "不再跟进"],
  ["rejected", "已挂"],
];

const COLUMN_LABELS_EN: Record<ApplicationStage, string> = {
  backlog: "Considering", applied: "Applied", written_test: "Assessment",
  interviewing: "Interviewing", offer: "Offer", pooled: "On hold",
  withdrawn: "Withdrawn", rejected: "Rejected",
};

export function localizedColumns(locale: UiLocale): readonly (readonly [ApplicationStage, string])[] {
  return locale === "en" ? COLUMNS.map(([stage]) => [stage, COLUMN_LABELS_EN[stage]] as const) : COLUMNS;
}

export const RAIL_STAGES: readonly ApplicationStage[] = ["pooled", "withdrawn", "rejected"];
export const TERMINAL_STAGES: readonly ApplicationStage[] = ["withdrawn", "rejected"];

export function nextStageAfterCurrentStageChange(
  currentNextStage: ApplicationStage,
  nextCurrentStage: ApplicationStage,
  nextStageManuallyEdited: boolean,
): ApplicationStage {
  // The editor hides next-action fields in a terminal current stage, but keeps
  // the unsaved draft so an accidental stage choice is fully reversible.
  if (TERMINAL_STAGES.includes(nextCurrentStage)) return currentNextStage;
  return nextStageManuallyEdited ? currentNextStage : nextCurrentStage;
}

export const STAGE_STYLES: Record<ApplicationStage, string> = {
  offer: "bg-ok-soft text-ok",
  withdrawn: "bg-panel-2 text-ink-2",
  rejected: "bg-panel-2 text-ink-3",
  interviewing: "bg-info-soft text-info",
  backlog: "bg-panel-2 text-ink-3",
  written_test: "bg-warn-soft text-warn",
  applied: "bg-panel-2 text-ink-2",
  pooled: "bg-ember-soft text-ember",
};

export const STAGE_DOT: Record<ApplicationStage, string> = {
  offer: "bg-ok",
  withdrawn: "bg-ink-3",
  interviewing: "bg-info",
  written_test: "bg-warn",
  applied: "bg-ink-3",
  backlog: "bg-ink-3",
  rejected: "bg-ink-3",
  pooled: "bg-ember",
};

export const OUTCOME_LABELS: Record<Exclude<TimelineEntry["outcome"], null>, string> = {
  passed: "通过",
  failed: "未通过",
  cancelled: "取消",
};

const OUTCOME_LABELS_EN: Record<Exclude<TimelineEntry["outcome"], null>, string> = {
  passed: "Passed",
  failed: "Not passed",
  cancelled: "Cancelled",
};

export function outcomeLabel(outcome: Exclude<TimelineEntry["outcome"], null>, locale: UiLocale = "zh-CN"): string {
  return locale === "en" ? OUTCOME_LABELS_EN[outcome] : OUTCOME_LABELS[outcome];
}

export function stageLabel(stage: ApplicationStage | string, locale: UiLocale = "zh-CN"): string {
  return localizedColumns(locale).find(([key]) => key === stage)?.[1] ?? stage;
}

type TimelineSummaryEntry = Pick<
  TimelineEntry,
  "step" | "summary" | "from_stage" | "to_stage" | "source"
>;

export function timelineEntrySummary(
  entry: TimelineSummaryEntry,
  locale: UiLocale = "zh-CN",
): string | null {
  const summary = entry.summary;
  if (!summary) return null;

  const fromStageZh = stageLabel(entry.from_stage);
  const toStageZh = stageLabel(entry.to_stage);
  if (
    entry.source === "agent"
    && entry.from_stage === "backlog"
    && summary === `批量导入新增岗位并设为「${toStageZh}」`
  ) {
    return locale === "en"
      ? `Added via bulk import and set to ${stageLabel(entry.to_stage, locale)}`
      : summary;
  }
  if (
    entry.source === "agent"
    && entry.from_stage === "backlog"
    && entry.to_stage === "applied"
    && summary === "投递"
  ) {
    return locale === "en" ? "Application submitted" : summary;
  }
  if (
    entry.source === "manual"
    && entry.from_stage === "backlog"
    && summary === `新增岗位并设为「${toStageZh}」`
  ) {
    return locale === "en"
      ? `Application added and set to ${stageLabel(entry.to_stage, locale)}`
      : summary;
  }
  if (
    entry.source === "drag"
    && summary === `从「${fromStageZh}」拖到「${toStageZh}」`
  ) {
    return locale === "en"
      ? `Moved from ${stageLabel(entry.from_stage, locale)} to ${stageLabel(entry.to_stage, locale)}`
      : summary;
  }
  if (
    entry.source === "manual"
    && summary === `阶段从「${fromStageZh}」调整为「${toStageZh}」`
  ) {
    return locale === "en"
      ? `Stage changed from ${stageLabel(entry.from_stage, locale)} to ${stageLabel(entry.to_stage, locale)}`
      : summary;
  }
  if (
    entry.source === "manual"
    && entry.step
    && summary === `完成「${entry.step}」`
  ) {
    return locale === "en" ? `Completed “${entry.step}”` : summary;
  }
  return summary;
}

export function timelineEntryTitle(
  entry: TimelineSummaryEntry,
  locale: UiLocale = "zh-CN",
): string {
  if (entry.step) return entry.step;
  if (entry.from_stage !== entry.to_stage) return locale === "en" ? `Moved to ${stageLabel(entry.to_stage, locale)}` : `进入${stageLabel(entry.to_stage)}`;
  const firstLine = timelineEntrySummary(entry, locale)?.split(/\r?\n/, 1)[0]?.trim();
  return firstLine || (locale === "en" ? "Progress update" : "进展更新");
}

export function timelineEntryNodeClass(
  entry: Pick<TimelineEntry, "outcome" | "to_stage">,
): string {
  if (entry.outcome === "failed" || entry.to_stage === "rejected") return "bg-bad";
  if (entry.outcome === "passed" || entry.to_stage === "offer") return "bg-ok";
  if (entry.to_stage === "interviewing") return "bg-info";
  if (entry.to_stage === "written_test") return "bg-warn";
  return "bg-ink-3";
}

export function normalizeTimelineQuery(value: string): string {
  return value.trim().normalize("NFKC").toLowerCase();
}

export function matchesTimelineQuery(
  item: Pick<BoardItem, "company" | "position" | "channel">,
  query: string,
): boolean {
  const normalizedQuery = normalizeTimelineQuery(query);
  if (!normalizedQuery) return true;
  return [item.company, item.position, item.channel ?? ""].some(
    (value) => normalizeTimelineQuery(value).includes(normalizedQuery),
  );
}

export function historyEntryIsMeaningful(
  entry: Pick<TimelineEntry, "step" | "summary" | "from_stage" | "to_stage"> & {
    outcome: TimelineEntry["outcome"] | "";
  },
): boolean {
  return Boolean(
    entry.step?.trim()
    || entry.outcome
    || entry.summary?.trim()
    || entry.from_stage !== entry.to_stage,
  );
}

export function todayIso(): string {
  const now = new Date();
  const month = String(now.getMonth() + 1).padStart(2, "0");
  const day = String(now.getDate()).padStart(2, "0");
  return `${now.getFullYear()}-${month}-${day}`;
}

export function dayDiff(fromIso: string, toIso: string): number {
  const from = Date.UTC(
    Number(fromIso.slice(0, 4)), Number(fromIso.slice(5, 7)) - 1, Number(fromIso.slice(8, 10)));
  const to = Date.UTC(
    Number(toIso.slice(0, 4)), Number(toIso.slice(5, 7)) - 1, Number(toIso.slice(8, 10)));
  return Math.round((to - from) / 86_400_000);
}

export function shortDate(iso: string): string {
  return `${Number(iso.slice(5, 7))}-${Number(iso.slice(8, 10))}`;
}

const EN_AGENDA_DATE_FORMATTER = new Intl.DateTimeFormat("en", {
  month: "short",
  day: "numeric",
  timeZone: "UTC",
});

export function agendaDateLabel(iso: string, locale: UiLocale = "zh-CN"): string {
  if (locale === "zh-CN") return `${Number(iso.slice(5, 7))}月${Number(iso.slice(8, 10))}日`;
  return EN_AGENDA_DATE_FORMATTER.format(new Date(`${iso}T00:00:00Z`));
}

const WEEKDAYS = ["周日", "周一", "周二", "周三", "周四", "周五", "周六"];
const WEEKDAYS_EN = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

export function weekdayLabel(iso: string, today: string, locale: UiLocale = "zh-CN"): string {
  const diff = dayDiff(today, iso);
  if (diff === 0) return locale === "en" ? "Today" : "今天";
  if (diff === 1) return locale === "en" ? "Tomorrow" : "明天";
  const day = new Date(`${iso}T00:00:00`).getDay();
  return (locale === "en" ? WEEKDAYS_EN : WEEKDAYS)[day] ?? "";
}

export function boardProgressLabel(item: BoardItem, locale: UiLocale = "zh-CN"): string | null {
  if (item.stage === "pooled") {
    const recovery = item.paused_from_stage
      ? locale === "en" ? `Can resume at ${stageLabel(item.paused_from_stage, locale)}` : `可恢复至${stageLabel(item.paused_from_stage)}`
      : locale === "en" ? "Can still resume" : "仍可恢复";
    const paused = locale === "en" ? "Process paused" : "流程暂停";
    return item.pause_reason ? `${paused} · ${item.pause_reason} · ${recovery}` : `${paused} · ${recovery}`;
  }
  if (item.stage === "withdrawn") return locale === "en" ? "Ended by you" : "已由你结束";
  if (item.stage === "rejected") return locale === "en" ? "Ended by the company" : "公司已结束流程";
  return item.current_step;
}

export type BoardCardMeta = {
  when: string | null;
  tone: "near" | "future" | "past" | null;
  what: string | null;
};

export function boardCardMeta(item: BoardItem, today: string, locale: UiLocale = "zh-CN"): BoardCardMeta {
  const nextAction = item.stage === "pooled" ? null : item.next_action;
  if (nextAction?.date && !TERMINAL_STAGES.includes(item.stage)) {
    const diff = dayDiff(today, nextAction.date);
    return {
      when: shortDate(nextAction.date),
      tone: diff < 0 ? "past" : diff <= 2 ? "near" : "future",
      what: nextAction.step,
    };
  }
  const progress = boardProgressLabel(item, locale);
  if (progress) return { when: null, tone: null, what: progress };
  if (item.stage === "backlog") return { when: null, tone: null, what: locale === "en" ? "Not applied yet" : "还没投递" };
  if (item.stage === "applied") {
    const idleDays = item.last_activity_time
      ? dayDiff(item.last_activity_time.slice(0, 10), today)
      : 0;
    const what = idleDays >= 14
      ? locale === "en" ? `Applied · no response for ${idleDays} days` : `已投递 · ${idleDays} 天无回音`
      : item.channel ? `${locale === "en" ? "Applied" : "已投递"} · ${item.channel}` : locale === "en" ? "Applied" : "已投递";
    return {
      when: item.applied_date ? shortDate(item.applied_date) : null,
      tone: item.applied_date ? "past" : null,
      what,
    };
  }
  return { when: null, tone: null, what: null };
}

export type UpcomingDayGroup = {
  date: string;
  dow: string;
  dateLabel: string;
  isNear: boolean;
  items: BoardItem[];
};

export function upcomingDayGroups(items: BoardItem[], today: string, locale: UiLocale = "zh-CN"): UpcomingDayGroup[] {
  const byDate = new Map<string, BoardItem[]>();
  for (const item of items) {
    const date = item.next_action?.date;
    if (!date || RAIL_STAGES.includes(item.stage)) continue;
    const group = byDate.get(date);
    if (group) group.push(item);
    else byDate.set(date, [item]);
  }
  return [...byDate.entries()]
    .sort(([a], [b]) => a.localeCompare(b))
    .map(([date, group]) => ({
      date,
      dow: weekdayLabel(date, today, locale),
      dateLabel: agendaDateLabel(date, locale),
      isNear: (() => { const diff = dayDiff(today, date); return diff >= 0 && diff <= 2; })(),
      items: sortByPriorityAndCreatedTime(group),
    }));
}

export function upcomingActionLabel(item: BoardItem, locale: UiLocale = "zh-CN"): string {
  return item.next_action?.step ?? (locale === "en" ? "Next action" : "下一步安排");
}

export function nextActionSummary(
  detail: Pick<BoardItem, "stage" | "next_action">,
  today: string,
  locale: UiLocale = "zh-CN",
): string | null {
  if (RAIL_STAGES.includes(detail.stage) || !detail.next_action) return null;
  const parts = [locale === "en" ? "Next" : "下一步", detail.next_action.step];
  if (detail.next_action.date) {
    parts.push(`${shortDate(detail.next_action.date)} ${weekdayLabel(detail.next_action.date, today, locale)}`);
  }
  if (detail.next_action.time) parts.push(detail.next_action.time);
  return parts.join(" · ");
}

export function timelineEntryDateLabel(
  entry: Pick<TimelineEntry, "occurred_date" | "created_time">,
  today: string,
  locale: UiLocale = "zh-CN",
): string {
  const date = entry.occurred_date ?? entry.created_time.slice(0, 10);
  const weekday = weekdayLabel(date, today, locale);
  return weekday ? `${shortDate(date)} ${weekday}` : shortDate(date);
}

export function sortListRows(rows: BoardItem[]): BoardItem[] {
  return sortByPriorityAndCreatedTime(rows);
}
