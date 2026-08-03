import type {
  ApplicationNextAction,
  ApplicationPriority,
  ApplicationStage,
  ApplicationTimelineEntry,
} from "../applications/applicationContract";

export type { ApplicationStage } from "../applications/applicationContract";

export type TimelineOutcome = "passed" | "failed" | "cancelled" | null;

export type NextAction = ApplicationNextAction;

export type BoardItem = {
  id: number;
  company: string;
  position: string;
  department: string | null;
  stage: ApplicationStage;
  current_step: string | null;
  next_action: NextAction | null;
  revision: number;
  priority: ApplicationPriority;
  applied_date: string | null;
  prep_status: string;
  last_activity_time: string | null;
  created_time: string;
  channel: string | null;
  pause_reason: string | null;
  paused_from_stage: ApplicationStage | null;
};

type Board = { columns: Record<ApplicationStage, BoardItem[]>; total: number };

type Upcoming = { days: number; items: BoardItem[] };

type TimelineStatistics = {
  total_positions: number;
  submitted: number;
  active_processes: number;
  offers: number;
  rejected: number;
  withdrawn: number;
  pooled: number;
  interview_conversion_percent: number;
  offer_conversion_percent: number;
  funnel: {
    submitted: number;
    written_test: number;
    interviewing: number;
    offer: number;
    rejected: number;
  };
};

export type TimelineEntry = Omit<ApplicationTimelineEntry, "journal_id"> & {
  display_time: string;
  snapshot_fingerprint: string;
};

type ApplicationDetail = BoardItem & {
  jd_text: string | null;
  jd_parsed: { skills: string[]; highlights: string[] };
  resume_id: number | null;
  updated_time: string;
  timeline_entries: TimelineEntry[];
  application_note: string | null;
  prep: Record<string, unknown> | null;
  prep_retry_after_seconds: number | null;
};

const APPLICATION_STAGE_KEYS: readonly ApplicationStage[] = [
  "backlog",
  "applied",
  "written_test",
  "interviewing",
  "offer",
  "withdrawn",
  "rejected",
  "pooled",
];
const APPLICATION_STAGES = new Set<ApplicationStage>(APPLICATION_STAGE_KEYS);
const ACTIVE_STAGES = new Set<ApplicationStage>([
  "backlog",
  "applied",
  "written_test",
  "interviewing",
  "offer",
]);
const TIMELINE_OUTCOMES = new Set<Exclude<TimelineOutcome, null>>([
  "passed",
  "failed",
  "cancelled",
]);
const TIMELINE_SOURCES = new Set<TimelineEntry["source"]>([
  "manual",
  "agent",
  "review",
  "drag",
  "system",
]);
const SNAPSHOT_FINGERPRINT_PATTERN = /^[0-9a-f]{64}$/;
const BOARD_KEYS = ["columns", "total"] as const;
const UPCOMING_KEYS = ["days", "items"] as const;
const STATISTICS_KEYS = [
  "total_positions",
  "submitted",
  "active_processes",
  "offers",
  "rejected",
  "withdrawn",
  "pooled",
  "interview_conversion_percent",
  "offer_conversion_percent",
  "funnel",
] as const;
const FUNNEL_KEYS = ["submitted", "written_test", "interviewing", "offer", "rejected"] as const;
const BOARD_ITEM_KEYS = [
  "id",
  "company",
  "position",
  "department",
  "channel",
  "stage",
  "current_step",
  "next_action",
  "paused_from_stage",
  "pause_reason",
  "priority",
  "applied_date",
  "prep_status",
  "revision",
  "last_activity_time",
  "created_time",
] as const;
const APPLICATION_DETAIL_KEYS = [
  "id",
  "company",
  "position",
  "department",
  "channel",
  "stage",
  "current_step",
  "next_action",
  "paused_from_stage",
  "pause_reason",
  "priority",
  "applied_date",
  "prep_status",
  "revision",
  "last_activity_time",
  "created_time",
  "jd_text",
  "jd_parsed",
  "resume_id",
  "updated_time",
  "prep",
  "prep_retry_after_seconds",
  "timeline_entries",
  "application_note",
] as const;
const NEXT_ACTION_KEYS = ["stage", "step", "date", "time", "note"] as const;
const JD_PARSED_KEYS = ["skills", "highlights"] as const;
const TIMELINE_ENTRY_KEYS = [
  "id",
  "step",
  "occurred_date",
  "outcome",
  "summary",
  "from_stage",
  "from_step",
  "to_stage",
  "to_step",
  "source",
  "created_time",
  "display_time",
  "snapshot_fingerprint",
] as const;

function isPlainRecord(value: unknown): value is Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function hasExactKeys(
  value: Record<string, unknown>,
  expected: readonly string[],
): boolean {
  const actual = Object.keys(value);
  return actual.length === expected.length
    && expected.every((key) => Object.hasOwn(value, key));
}

function isStringOrNull(value: unknown): value is string | null {
  return value === null || typeof value === "string";
}

function isPositiveIntegerOrNull(value: unknown): value is number | null {
  return value === null || (Number.isInteger(value) && (value as number) > 0);
}

function isNonNegativeIntegerOrNull(value: unknown): value is number | null {
  return value === null || (Number.isInteger(value) && (value as number) >= 0);
}

function isIsoDate(value: unknown): value is string {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const [year, month, day] = value.split("-").map(Number);
  const parsed = new Date(Date.UTC(year, month - 1, day));
  return parsed.getUTCFullYear() === year
    && parsed.getUTCMonth() === month - 1
    && parsed.getUTCDate() === day;
}

function isStage(value: unknown): value is ApplicationStage {
  return typeof value === "string" && APPLICATION_STAGES.has(value as ApplicationStage);
}

function isNextAction(value: unknown): value is NextAction {
  if (!isPlainRecord(value) || !hasExactKeys(value, NEXT_ACTION_KEYS)) return false;
  return isStage(value.stage)
    && typeof value.step === "string"
    && value.step.trim().length > 0
    && (value.date === null || isIsoDate(value.date))
    && (value.time === null
      || (typeof value.time === "string" && /^(?:[01]\d|2[0-3]):[0-5]\d$/.test(value.time)))
    && (value.time === null || value.date !== null)
    && isStringOrNull(value.note);
}

function isJdParsed(value: unknown): value is ApplicationDetail["jd_parsed"] {
  if (!isPlainRecord(value) || !hasExactKeys(value, JD_PARSED_KEYS)) return false;
  return Array.isArray(value.skills)
    && value.skills.every((skill) => typeof skill === "string")
    && Array.isArray(value.highlights)
    && value.highlights.every((highlight) => typeof highlight === "string");
}

function hasValidBoardItemFields(value: Record<string, unknown>): boolean {
  const stage = value.stage;
  const pausedFromStage = value.paused_from_stage;
  const pauseReason = value.pause_reason;
  return Number.isInteger(value.id)
    && (value.id as number) > 0
    && typeof value.company === "string"
    && typeof value.position === "string"
    && isStringOrNull(value.department)
    && isStringOrNull(value.channel)
    && isStage(stage)
    && isStringOrNull(value.current_step)
    && (value.next_action === null || isNextAction(value.next_action))
    && (stage !== "rejected" && stage !== "withdrawn" || value.next_action === null)
    && (stage === "pooled"
      ? (pausedFromStage === null
        || (isStage(pausedFromStage) && ACTIVE_STAGES.has(pausedFromStage)))
      : pausedFromStage === null && pauseReason === null)
    && (pauseReason === null
      || (typeof pauseReason === "string"
        && pauseReason.trim() === pauseReason
        && pauseReason.length > 0
        && Array.from(pauseReason).length <= 1_000))
    && (value.priority === null || value.priority === "high"
      || value.priority === "medium" || value.priority === "low")
    && (value.applied_date === null || isIsoDate(value.applied_date))
    && typeof value.prep_status === "string"
    && Number.isInteger(value.revision)
    && (value.revision as number) >= 0
    && isStringOrNull(value.last_activity_time)
    && typeof value.created_time === "string"
    && value.created_time.length > 0;
}

function isBoardItem(value: unknown): value is BoardItem {
  return isPlainRecord(value)
    && hasExactKeys(value, BOARD_ITEM_KEYS)
    && hasValidBoardItemFields(value);
}

function isBoardColumns(
  value: unknown,
): value is Record<ApplicationStage, BoardItem[]> {
  if (!isPlainRecord(value) || !hasExactKeys(value, APPLICATION_STAGE_KEYS)) return false;
  return APPLICATION_STAGE_KEYS.every((stage) => {
    const items = value[stage];
    return Array.isArray(items)
      && items.every((item) => isBoardItem(item) && item.stage === stage);
  });
}

function parseBoard(value: unknown): Board {
  if (!isPlainRecord(value)
      || !hasExactKeys(value, BOARD_KEYS)
      || !isBoardColumns(value.columns)
      || !Number.isInteger(value.total)
      || (value.total as number) < 0) {
    throw new TypeError("时间线看板数据格式异常，请刷新后重试");
  }
  const columns = value.columns as Record<ApplicationStage, BoardItem[]>;
  const itemCount = APPLICATION_STAGE_KEYS.reduce(
    (total, stage) => total + columns[stage].length,
    0,
  );
  if (value.total !== itemCount) {
    throw new TypeError("时间线看板数据数量不一致，请刷新后重试");
  }
  const applicationIds = APPLICATION_STAGE_KEYS.flatMap(
    (stage) => columns[stage].map((item) => item.id),
  );
  if (new Set(applicationIds).size !== applicationIds.length) {
    throw new TypeError("时间线看板包含重复岗位，请刷新后重试");
  }
  return value as Board;
}

function parseUpcoming(value: unknown): Upcoming {
  if (!isPlainRecord(value)
      || !hasExactKeys(value, UPCOMING_KEYS)
      || !Number.isInteger(value.days)
      || (value.days as number) < 1
      || (value.days as number) > 60
      || !Array.isArray(value.items)
      || !value.items.every(isBoardItem)) {
    throw new TypeError("未来安排数据格式异常，请刷新后重试");
  }
  const items = value.items as BoardItem[];
  if (new Set(items.map((item) => item.id)).size !== items.length) {
    throw new TypeError("未来安排包含重复岗位，请刷新后重试");
  }
  return value as Upcoming;
}

function isNonNegativeInteger(value: unknown): value is number {
  return Number.isInteger(value) && (value as number) >= 0;
}

function isPercentage(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 100;
}

function parseTimelineStatistics(value: unknown): TimelineStatistics {
  if (!isPlainRecord(value)
      || !hasExactKeys(value, STATISTICS_KEYS)) {
    throw new TypeError("求职统计数据格式异常，请稍后重试");
  }
  const funnel = value.funnel;
  if (!isPlainRecord(funnel)
      || !hasExactKeys(funnel, FUNNEL_KEYS)
      || !STATISTICS_KEYS.slice(0, 7).every((key) => isNonNegativeInteger(value[key]))
      || !FUNNEL_KEYS.every((key) => isNonNegativeInteger(funnel[key]))
      || !isPercentage(value.interview_conversion_percent)
      || !isPercentage(value.offer_conversion_percent)) {
    throw new TypeError("求职统计数据格式异常，请稍后重试");
  }
  const submitted = value.submitted as number;
  const totalPositions = value.total_positions as number;
  if (submitted > totalPositions || funnel.submitted !== submitted) {
    throw new TypeError("求职统计数据格式异常，请稍后重试");
  }
  return value as TimelineStatistics;
}

function isTimelineEntry(value: unknown): value is TimelineEntry {
  if (!isPlainRecord(value) || !hasExactKeys(value, TIMELINE_ENTRY_KEYS)) return false;
  return Number.isInteger(value.id)
    && (value.id as number) > 0
    && isStringOrNull(value.step)
    && (value.occurred_date === null || isIsoDate(value.occurred_date))
    && (value.outcome === null
      || (typeof value.outcome === "string"
        && TIMELINE_OUTCOMES.has(value.outcome as Exclude<TimelineOutcome, null>)))
    && isStringOrNull(value.summary)
    && isStage(value.from_stage)
    && isStringOrNull(value.from_step)
    && isStage(value.to_stage)
    && isStringOrNull(value.to_step)
    && typeof value.source === "string"
    && TIMELINE_SOURCES.has(value.source as TimelineEntry["source"])
    && typeof value.created_time === "string"
    && typeof value.display_time === "string"
    && typeof value.snapshot_fingerprint === "string"
    && SNAPSHOT_FINGERPRINT_PATTERN.test(value.snapshot_fingerprint);
}

function parseApplicationDetail(value: unknown): ApplicationDetail {
  if (!isPlainRecord(value)
      || !hasExactKeys(value, APPLICATION_DETAIL_KEYS)
      || !hasValidBoardItemFields(value)
      || !isStringOrNull(value.jd_text)
      || !isJdParsed(value.jd_parsed)
      || !isPositiveIntegerOrNull(value.resume_id)
      || typeof value.updated_time !== "string"
      || (value.prep !== null && !isPlainRecord(value.prep))
      || !isNonNegativeIntegerOrNull(value.prep_retry_after_seconds)
      || !Array.isArray(value.timeline_entries)
      || !value.timeline_entries.every(isTimelineEntry)
      || !isStringOrNull(value.application_note)) {
    throw new TypeError("岗位详情数据格式异常，请刷新后重试");
  }
  return value as ApplicationDetail;
}

const ApplicationDetail = Object.freeze({ parse: parseApplicationDetail });
const Board = Object.freeze({ parse: parseBoard, parseUpcoming });
const TimelineStatistics = Object.freeze({ parse: parseTimelineStatistics });

export { ApplicationDetail, Board, TimelineStatistics };
