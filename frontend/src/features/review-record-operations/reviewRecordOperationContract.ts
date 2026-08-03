import type { ReviewUndoOperation } from "../review-operations/reviewUndoOperationContract";
import type {
  ApplicationNextAction,
  ApplicationStage,
} from "../applications/applicationContract";
import { normalizeApplicationIdentityPart } from "../applications/applicationIdentity.ts";

export type ReviewRecordMode = "initial" | "supplement";
export type ReviewRecordState =
  | "processing"
  | "pending_confirmation"
  | "completed"
  | "rejected"
  | "failed"
  | "superseded";
export type ReviewRecordOutcome = "applied" | "needs_clarification";
export type ReviewRecordApplicationStage = ApplicationStage;
export type ReviewRecordNextAction = ApplicationNextAction;

export type ReviewRecordHistory = {
  step: string | null;
  date: string | null;
  outcome: "passed" | "failed" | "cancelled" | null;
  summary: string | null;
};

export type ReviewRecordProjectedState = {
  stage: ReviewRecordApplicationStage | null;
  current_step: string | null;
};

export type ReviewRecordProjectionSnapshot = {
  stage: ReviewRecordApplicationStage;
  current_step: string | null;
  current_state_entry_id: number | null;
  next_action: ReviewRecordNextAction | null;
  paused_from_stage: ReviewRecordApplicationStage | null;
  pause_reason: string | null;
  channel: string | null;
  applied_date: string | null;
  revision: number;
};

export type ReviewRecordExtraction = {
  company: string | null;
  position: string | null;
  channel: string | null;
  history: ReviewRecordHistory | null;
  projected_state: ReviewRecordProjectedState | null;
  clear_next_action: boolean;
  next_action: ReviewRecordNextAction | null;
  questions: {
    text: string;
    stuck: boolean;
    knowledge_points: string[];
  }[];
  mood: string | null;
  time_of_day: "morning" | "afternoon" | "evening" | null;
  factors: string[];
};

export type ReviewRecordMissingField = {
  field: "company" | "position";
  ask: string;
};

export type ReviewRecordDerivation = {
  application_id: number;
  application_created: boolean;
  timeline_entry_ids: number[];
  question_ids: number[];
  knowledge_point_ids: number[];
  status_log_ids: number[];
  application_before: ReviewRecordProjectionSnapshot;
  application_after: ReviewRecordProjectionSnapshot;
  revision: number;
};

type ReviewRecordTargetPlanBase = {
  company: string;
  position: string;
  current_stage: ReviewRecordApplicationStage;
  current_step: string | null;
  projected_stage: ReviewRecordApplicationStage;
  projected_step: string | null;
  current_next_action: ReviewRecordNextAction | null;
  projected_next_action: ReviewRecordNextAction | null;
  current_applied_date: string | null;
  projected_applied_date: string | null;
  current_channel: string | null;
  projected_channel: string | null;
};

export type ReviewRecordTargetPlan = (ReviewRecordTargetPlanBase & {
  kind: "existing";
  application_id: number;
  created_time: string;
  revision: number;
}) | (ReviewRecordTargetPlanBase & {
  kind: "new";
  current_stage: "backlog";
  current_step: null;
  current_next_action: null;
  current_applied_date: null;
  current_channel: null;
});

export type ReviewRecordResult = {
  outcome: ReviewRecordOutcome;
  review_reference: string;
  source_journal_id: number;
  target_journal_id: number;
  target_revision: number;
  extraction: ReviewRecordExtraction;
  missing: ReviewRecordMissingField[];
  derivation: ReviewRecordDerivation | null;
  application: { id: number; company: string; position: string } | null;
};

export type ReviewRecordPreview = {
  extraction: ReviewRecordExtraction;
  target_plan: ReviewRecordTargetPlan | null;
  missing: ReviewRecordMissingField[];
};

export type ReviewRecordOperation = {
  operation_type: "review_record";
  contract_version: 1;
  operation_id: string;
  review_reference: string;
  client_turn_id: string;
  mode: ReviewRecordMode;
  state: ReviewRecordState;
  terminal: boolean;
  outcome: ReviewRecordOutcome | null;
  created_time: string;
  finished_time: string | null;
  source_journal_id: number;
  target_journal_id: number;
  target_current_state:
    | "pending"
    | "awaiting_user"
    | "applied"
    | "failed"
    | "superseded"
    | "voided";
  target_current_revision: number;
  preview: ReviewRecordPreview | null;
  result: ReviewRecordResult | null;
  error: {
    code:
      | "extract_failed"
      | "extract_cancelled"
      | "publish_failed"
      | "interrupted"
      | "target_changed"
      | "source_changed"
      | "contract_invalid";
    message: string;
  } | null;
  undo_available: boolean;
  undo_block_reason:
    | "operation_not_applied"
    | "target_changed"
    | "target_not_applied"
    | null;
};

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const VALID_STAGES = new Set([
  "backlog", "applied", "written_test", "interviewing",
  "offer", "withdrawn", "rejected", "pooled",
]);
const ACTIVE_STAGES = new Set<ReviewRecordApplicationStage>([
  "backlog", "applied", "written_test", "interviewing", "offer",
]);
const VALID_TARGET_STATES = new Set([
  "pending", "awaiting_user", "applied", "failed", "superseded", "voided",
]);
const VALID_ERROR_CODES = new Set([
  "extract_failed", "extract_cancelled", "publish_failed", "interrupted",
  "target_changed", "source_changed", "contract_invalid",
]);
const VALID_UNDO_BLOCK_REASONS = new Set([
  "operation_not_applied", "target_changed", "target_not_applied",
]);
const MAX_REVIEW_TOTAL_TEXT_CHARS = 50_000;

export const REVIEW_RECORD_TARGET_STATE_LABELS: Record<
  ReviewRecordOperation["target_current_state"],
  string
> = {
  pending: "提取中",
  awaiting_user: "等待补充",
  applied: "已发布",
  failed: "处理失败",
  superseded: "已被取代",
  voided: "已撤销",
};

export const REVIEW_RECORD_UNDO_BLOCK_LABELS: Record<
  NonNullable<ReviewRecordOperation["undo_block_reason"]>,
  string
> = {
  operation_not_applied: "本次操作没有发布复盘，因此没有可撤销内容。",
  target_changed: "目标复盘后来又被修改；旧收据不能覆盖新版本，请以当前记录为准。",
  target_not_applied: "目标复盘已撤销或不再处于已发布状态，不能再次生成撤销预览。",
};

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function hasExactKeys(value: Record<string, unknown>, keys: readonly string[]): boolean {
  const actual = Object.keys(value);
  return actual.length === keys.length && keys.every((key) => actual.includes(key));
}

function codePointLength(value: string): number {
  return Array.from(value).length;
}

function boundedText(value: unknown, maxLength: number): value is string {
  return typeof value === "string"
    && codePointLength(value) > 0
    && codePointLength(value) <= maxLength
    && value.trim() === value;
}

function nullableText(value: unknown, maxLength: number): boolean {
  return value === null || boundedText(value, maxLength);
}

function sameNextAction(
  left: ReviewRecordNextAction | null,
  right: ReviewRecordNextAction | null,
): boolean {
  return left === null || right === null
    ? left === right
    : left.stage === right.stage
      && left.step === right.step
      && left.date === right.date
      && left.time === right.time
      && left.note === right.note;
}

function isUuid(value: unknown): value is string {
  return typeof value === "string" && UUID_PATTERN.test(value);
}

function isPositiveInteger(value: unknown): value is number {
  return Number.isInteger(value) && (value as number) > 0;
}

function isNonNegativeInteger(value: unknown): value is number {
  return Number.isInteger(value) && (value as number) >= 0;
}

function isIsoDate(value: unknown): value is string {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const [year, month, day] = value.split("-").map(Number);
  const parsed = new Date(Date.UTC(year, month - 1, day));
  return parsed.getUTCFullYear() === year
    && parsed.getUTCMonth() === month - 1
    && parsed.getUTCDate() === day;
}

function optionalIsoDate(value: unknown): boolean {
  return value === null || isIsoDate(value);
}

function isStage(value: unknown): value is ReviewRecordApplicationStage {
  return typeof value === "string" && VALID_STAGES.has(value);
}

function isNextAction(value: unknown): value is ReviewRecordNextAction {
  return isRecord(value)
    && hasExactKeys(value, ["stage", "step", "date", "time", "note"])
    && isStage(value.stage)
    && boundedText(value.step, 300)
    && optionalIsoDate(value.date)
    && (value.time === null || (typeof value.time === "string" && /^([01]\d|2[0-3]):[0-5]\d$/.test(value.time)))
    && nullableText(value.note, 2_000)
    && (value.time === null || value.date !== null);
}

function isHistory(value: unknown): value is ReviewRecordHistory {
  return isRecord(value)
    && hasExactKeys(value, ["step", "date", "outcome", "summary"])
    && nullableText(value.step, 300)
    && optionalIsoDate(value.date)
    && (value.outcome === null || value.outcome === "passed" || value.outcome === "failed" || value.outcome === "cancelled")
    && nullableText(value.summary, 2_000)
    && (value.step !== null || value.outcome !== null || value.summary !== null);
}

function isProjectedState(value: unknown): value is ReviewRecordProjectedState {
  return isRecord(value)
    && hasExactKeys(value, ["stage", "current_step"])
    && (value.stage === null || isStage(value.stage))
    && nullableText(value.current_step, 300)
    && (value.stage !== null || value.current_step !== null);
}

function isExtraction(value: unknown): value is ReviewRecordExtraction {
  if (!isRecord(value) || !hasExactKeys(value, [
    "company", "position", "channel", "history", "projected_state",
    "clear_next_action", "next_action", "questions", "mood", "time_of_day", "factors",
  ]) || !nullableText(value.company, 200)
      || !nullableText(value.position, 300)
      || !nullableText(value.channel, 100)
      || (value.history !== null && !isHistory(value.history))
      || (value.projected_state !== null && !isProjectedState(value.projected_state))
      || typeof value.clear_next_action !== "boolean"
      || (value.next_action !== null && !isNextAction(value.next_action))
      || (value.clear_next_action && value.next_action !== null)
      || (value.history === null && value.projected_state === null
        && !value.clear_next_action && value.next_action === null)
      || !Array.isArray(value.questions) || value.questions.length > 50
      || !nullableText(value.mood, 1_000)
      || (value.time_of_day !== null && !["morning", "afternoon", "evening"].includes(String(value.time_of_day)))
      || !Array.isArray(value.factors) || value.factors.length > 20) return false;
  const extraction = value as unknown as ReviewRecordExtraction;
  if ((extraction.projected_state?.stage === "withdrawn"
      || extraction.projected_state?.stage === "rejected")
      && extraction.next_action !== null) return false;
  const questionTexts = new Set<string>();
  for (const question of value.questions) {
    if (!isRecord(question)
        || !hasExactKeys(question, ["text", "stuck", "knowledge_points"])
        || !boundedText(question.text, 4_000)
        || typeof question.stuck !== "boolean"
        || !Array.isArray(question.knowledge_points)
        || question.knowledge_points.length > 3
        || questionTexts.has(question.text)) return false;
    questionTexts.add(question.text);
    const points = new Set<string>();
    for (const point of question.knowledge_points) {
      if (!boundedText(point, 100) || points.has(point)) return false;
      points.add(point);
    }
  }
  const factors = new Set<string>();
  for (const factor of value.factors) {
    if (!boundedText(factor, 200) || factors.has(factor)) return false;
    factors.add(factor);
  }
  const texts: (string | null)[] = [
    extraction.company,
    extraction.position,
    extraction.channel,
    extraction.history?.step ?? null,
    extraction.history?.date ?? null,
    extraction.history?.summary ?? null,
    extraction.projected_state?.current_step ?? null,
    extraction.next_action?.step ?? null,
    extraction.next_action?.date ?? null,
    extraction.next_action?.time ?? null,
    extraction.next_action?.note ?? null,
    extraction.mood,
    ...extraction.factors,
    ...extraction.questions.flatMap((question) => [
      question.text,
      ...question.knowledge_points,
    ]),
  ];
  return texts.reduce((total, item) => total + (
    item === null ? 0 : codePointLength(item)
  ), 0) <= MAX_REVIEW_TOTAL_TEXT_CHARS;
}

function isMissingList(value: unknown): value is ReviewRecordMissingField[] {
  if (!Array.isArray(value) || value.length > 2) return false;
  const fields = new Set<string>();
  for (const item of value) {
    if (!isRecord(item)
        || !hasExactKeys(item, ["field", "ask"])
        || (item.field !== "company" && item.field !== "position")
        || !boundedText(item.ask, 8_000)
        || fields.has(item.field)) return false;
    fields.add(item.field);
  }
  return true;
}

function isTargetPlan(value: unknown): value is ReviewRecordTargetPlan {
  if (!isRecord(value)
      || (value.kind !== "existing" && value.kind !== "new")
      || !boundedText(value.company, 200)
      || !boundedText(value.position, 300)
      || !isStage(value.current_stage)
      || !nullableText(value.current_step, 300)
      || !isStage(value.projected_stage)
      || !nullableText(value.projected_step, 300)
      || (value.current_next_action !== null && !isNextAction(value.current_next_action))
      || (value.projected_next_action !== null && !isNextAction(value.projected_next_action))
      || !optionalIsoDate(value.current_applied_date)
      || !optionalIsoDate(value.projected_applied_date)
      || !nullableText(value.current_channel, 100)
      || !nullableText(value.projected_channel, 100)) return false;
  if (value.kind === "existing") {
    return hasExactKeys(value, [
      "kind", "application_id", "company", "position", "created_time", "revision",
      "current_stage", "current_step", "projected_stage", "projected_step",
      "current_next_action", "projected_next_action", "current_applied_date",
      "projected_applied_date", "current_channel", "projected_channel",
    ]) && isPositiveInteger(value.application_id)
      && boundedText(value.created_time, 64)
      && isNonNegativeInteger(value.revision);
  }
  return hasExactKeys(value, [
    "kind", "company", "position", "current_stage", "current_step", "projected_stage",
    "projected_step", "current_next_action", "projected_next_action", "current_applied_date",
    "projected_applied_date", "current_channel", "projected_channel",
  ]) && value.current_stage === "backlog"
    && value.current_step === null
    && value.current_next_action === null
    && value.current_applied_date === null
    && value.current_channel === null;
}

function isPreview(value: unknown): value is ReviewRecordPreview {
  if (!(isRecord(value)
    && hasExactKeys(value, ["extraction", "target_plan", "missing"])
    && isExtraction(value.extraction)
    && isMissingList(value.missing)
    && (value.target_plan === null ? value.missing.length > 0 : isTargetPlan(value.target_plan)))) {
    return false;
  }
  if (value.target_plan === null) return true;
  if (!isTargetPlan(value.target_plan)) return false;
  const extraction = value.extraction;
  const plan = value.target_plan;
  if (value.missing.length !== 0) return false;
  if (normalizeApplicationIdentityPart(extraction.company)
      !== normalizeApplicationIdentityPart(plan.company)) return false;
  if (plan.kind === "new" && normalizeApplicationIdentityPart(extraction.position)
      !== normalizeApplicationIdentityPart(plan.position)) return false;
  if (extraction.clear_next_action && plan.current_next_action === null) return false;
  const projectedStage = extraction.projected_state?.stage ?? plan.current_stage;
  const projectedStep = extraction.projected_state?.current_step ?? plan.current_step;
  if (plan.projected_stage !== projectedStage || plan.projected_step !== projectedStep) return false;
  if (projectedStage === "withdrawn" || projectedStage === "rejected") {
    if (extraction.next_action !== null) return false;
    if (plan.current_next_action !== null && !extraction.clear_next_action) return false;
  }
  const projectedNext = projectedStage === "withdrawn" || projectedStage === "rejected"
    ? null
    : extraction.clear_next_action
      ? null
      : extraction.next_action ?? plan.current_next_action;
  if (!sameNextAction(plan.projected_next_action, projectedNext)) return false;
  if (extraction.history === null
      && !extraction.clear_next_action
      && extraction.next_action === null
      && projectedStage === plan.current_stage
      && projectedStep === plan.current_step) return false;
  const projectedAppliedDate = plan.current_stage !== "applied"
      && extraction.projected_state?.stage === "applied"
      && plan.current_applied_date === null
    ? extraction.history?.date ?? null
    : plan.current_applied_date;
  return plan.projected_applied_date === projectedAppliedDate
    && plan.projected_channel === (extraction.channel || plan.current_channel);
}

function isPositiveUniqueIntegerList(value: unknown, maxLength: number, minLength = 0): value is number[] {
  return Array.isArray(value)
    && value.length >= minLength
    && value.length <= maxLength
    && value.every(isPositiveInteger)
    && new Set(value).size === value.length;
}

function isProjectionSnapshot(value: unknown): value is ReviewRecordProjectionSnapshot {
  if (!(isRecord(value)
    && hasExactKeys(value, [
      "stage", "current_step", "current_state_entry_id", "next_action",
      "paused_from_stage", "pause_reason", "channel", "applied_date", "revision",
    ])
    && isStage(value.stage)
    && nullableText(value.current_step, 300)
    && (value.current_state_entry_id === null || isPositiveInteger(value.current_state_entry_id))
    && (value.next_action === null || isNextAction(value.next_action))
    && (value.paused_from_stage === null || isStage(value.paused_from_stage))
    && nullableText(value.pause_reason, 1_000)
    && nullableText(value.channel, 100)
    && optionalIsoDate(value.applied_date)
    && isNonNegativeInteger(value.revision))) return false;
  const snapshot = value as unknown as ReviewRecordProjectionSnapshot;
  if ((snapshot.stage === "withdrawn" || snapshot.stage === "rejected")
      && snapshot.next_action !== null) return false;
  if (snapshot.stage !== "pooled") {
    return snapshot.paused_from_stage === null && snapshot.pause_reason === null;
  }
  return snapshot.paused_from_stage === null || ACTIVE_STAGES.has(snapshot.paused_from_stage);
}

function isDerivation(value: unknown): value is ReviewRecordDerivation {
  return isRecord(value)
    && hasExactKeys(value, [
      "application_id", "application_created", "timeline_entry_ids", "question_ids",
      "knowledge_point_ids", "status_log_ids", "application_before",
      "application_after", "revision",
    ])
    && isPositiveInteger(value.application_id)
    && typeof value.application_created === "boolean"
    && isPositiveUniqueIntegerList(value.timeline_entry_ids, 1, 1)
    && isPositiveUniqueIntegerList(value.question_ids, 50)
    && isPositiveUniqueIntegerList(value.knowledge_point_ids, 150)
    && value.knowledge_point_ids.every((item, index, values) => (
      index === 0 || values[index - 1] < item
    ))
    && isPositiveUniqueIntegerList(value.status_log_ids, 1)
    && isProjectionSnapshot(value.application_before)
    && isProjectionSnapshot(value.application_after)
    && isPositiveInteger(value.revision);
}

function isResult(value: unknown): value is ReviewRecordResult {
  if (!isRecord(value) || !hasExactKeys(value, [
    "outcome", "review_reference", "source_journal_id", "target_journal_id",
    "target_revision", "extraction", "missing", "derivation", "application",
  ]) || (value.outcome !== "applied" && value.outcome !== "needs_clarification")
      || !isUuid(value.review_reference)
      || !isPositiveInteger(value.source_journal_id)
      || !isPositiveInteger(value.target_journal_id)
      || !isPositiveInteger(value.target_revision)
      || !isExtraction(value.extraction)
      || !isMissingList(value.missing)) return false;
  if (value.outcome === "needs_clarification") {
    return value.missing.length > 0 && value.derivation === null && value.application === null;
  }
  if (!(value.missing.length === 0
    && isDerivation(value.derivation)
    && value.derivation.revision === value.target_revision
    && isRecord(value.application)
    && hasExactKeys(value.application, ["id", "company", "position"])
    && isPositiveInteger(value.application.id)
    && value.application.id === value.derivation.application_id
    && boundedText(value.application.company, 200)
    && boundedText(value.application.position, 300))) return false;
  const extraction = value.extraction as ReviewRecordExtraction;
  const derivation = value.derivation as ReviewRecordDerivation;
  const application = value.application as ReviewRecordResult["application"] & {};
  const before = derivation.application_before;
  const after = derivation.application_after;
  const expectedStage = extraction.projected_state?.stage ?? before.stage;
  const expectedStep = extraction.projected_state?.current_step ?? before.current_step;
  const expectedNext = expectedStage === "withdrawn" || expectedStage === "rejected"
    ? null
    : extraction.clear_next_action
      ? null
      : extraction.next_action ?? before.next_action;
  const expectedAppliedDate = before.stage !== "applied"
      && extraction.projected_state?.stage === "applied"
      && before.applied_date === null
    ? extraction.history?.date ?? null
    : before.applied_date;
  const stateChanged = expectedStage !== before.stage || expectedStep !== before.current_step;
  const expectedStateEntryId = stateChanged
    ? derivation.timeline_entry_ids[0]
    : before.current_state_entry_id;
  const expectedPausedFrom = expectedStage === "pooled"
    ? ACTIVE_STAGES.has(before.stage) ? before.stage : before.paused_from_stage
    : null;
  const expectedPauseReason = expectedStage === "pooled" && before.stage === "pooled"
    ? before.pause_reason
    : null;
  if (after.stage !== expectedStage
      || after.current_step !== expectedStep
      || after.current_state_entry_id !== expectedStateEntryId
      || !sameNextAction(after.next_action, expectedNext)
      || after.paused_from_stage !== expectedPausedFrom
      || after.pause_reason !== expectedPauseReason
      || after.channel !== (extraction.channel || before.channel)
      || after.applied_date !== expectedAppliedDate
      || after.revision !== before.revision + 1
      || normalizeApplicationIdentityPart(extraction.company)
        !== normalizeApplicationIdentityPart(application.company)
      || (extraction.position !== null
        && normalizeApplicationIdentityPart(extraction.position)
          !== normalizeApplicationIdentityPart(application.position))) return false;
  return !derivation.application_created || (
    before.stage === "backlog"
    && before.current_step === null
    && before.current_state_entry_id === null
    && before.next_action === null
    && before.paused_from_stage === null
    && before.pause_reason === null
    && before.channel === null
    && before.applied_date === null
    && before.revision === 0
  );
}

export function isReviewRecordOperation(value: unknown): value is ReviewRecordOperation {
  if (!isRecord(value) || !hasExactKeys(value, [
    "operation_type", "contract_version", "operation_id", "review_reference", "client_turn_id",
    "mode", "state", "terminal", "outcome", "created_time", "finished_time",
    "source_journal_id", "target_journal_id", "target_current_state",
    "target_current_revision", "preview", "result", "error", "undo_available",
    "undo_block_reason",
  ]) || value.operation_type !== "review_record"
      || value.contract_version !== 1
      || !isUuid(value.operation_id)
      || !isUuid(value.review_reference)
      || !isUuid(value.client_turn_id)
      || (value.mode !== "initial" && value.mode !== "supplement")
      || !["processing", "pending_confirmation", "completed", "rejected", "failed", "superseded"].includes(String(value.state))
      || typeof value.terminal !== "boolean"
      || (value.outcome !== null && value.outcome !== "applied" && value.outcome !== "needs_clarification")
      || !boundedText(value.created_time, 64)
      || (value.finished_time !== null && !boundedText(value.finished_time, 64))
      || !isPositiveInteger(value.source_journal_id)
      || !isPositiveInteger(value.target_journal_id)
      || !VALID_TARGET_STATES.has(String(value.target_current_state))
      || !isNonNegativeInteger(value.target_current_revision)
      || typeof value.undo_available !== "boolean"
      || (value.undo_block_reason !== null && !VALID_UNDO_BLOCK_REASONS.has(String(value.undo_block_reason)))) return false;
  if (value.mode === "initial") {
    if (value.review_reference !== value.operation_id || value.source_journal_id !== value.target_journal_id) return false;
  } else if (value.source_journal_id === value.target_journal_id) return false;

  let validPayload = false;
  if (value.state === "processing") {
    validPayload = !value.terminal && value.outcome === null && value.finished_time === null
      && value.preview === null && value.result === null && value.error === null;
  } else if (value.state === "pending_confirmation") {
    validPayload = !value.terminal && value.outcome === null && value.finished_time === null
      && isPreview(value.preview) && value.result === null && value.error === null;
  } else if (value.terminal && value.finished_time !== null && value.state === "completed") {
    validPayload = value.preview === null && isResult(value.result) && value.error === null
      && value.outcome === value.result.outcome
      && value.result.review_reference === value.review_reference
      && value.result.source_journal_id === value.source_journal_id
      && value.result.target_journal_id === value.target_journal_id;
  } else if (value.terminal && value.finished_time !== null && value.state === "rejected") {
    validPayload = value.preview === null && value.result === null && value.outcome === null && value.error === null;
  } else if (value.terminal && value.finished_time !== null
      && (value.state === "failed" || value.state === "superseded")) {
    validPayload = value.preview === null && value.result === null && value.outcome === null
      && isRecord(value.error)
      && hasExactKeys(value.error, ["code", "message"])
      && VALID_ERROR_CODES.has(String(value.error.code))
      && boundedText(value.error.message, 500);
  }
  if (!validPayload) return false;

  const appliedResult = value.state === "completed" && value.outcome === "applied" && isResult(value.result)
    ? value.result
    : null;
  const expectedBlock = appliedResult === null
    ? "operation_not_applied"
    : value.target_current_state !== "applied"
      ? "target_not_applied"
      : value.target_current_revision !== appliedResult.target_revision
        ? "target_changed"
        : null;
  return value.undo_available === (expectedBlock === null) && value.undo_block_reason === expectedBlock;
}

export function isPreparedReviewUndoOperation(
  value: unknown,
  targetJournalId: number,
  expectedRevision: number,
): value is ReviewUndoOperation {
  return isRecord(value)
    && hasExactKeys(value, ["operation_id", "state", "created_time", "target", "effect", "result"])
    && isUuid(value.operation_id)
    && value.state === "pending"
    && boundedText(value.created_time, 64)
    && value.result === null
    && isRecord(value.target)
    && value.target.journal_id === targetJournalId
    && value.target.expected_revision === expectedRevision
    && isRecord(value.effect);
}

export function reviewRecordIntegrityIssue(operation: ReviewRecordOperation): string | null {
  if (operation.mode === "initial"
      && (operation.review_reference !== operation.operation_id
        || operation.source_journal_id !== operation.target_journal_id)) {
    return "初次复盘的稳定引用或来源身份不一致";
  }
  if (operation.mode === "supplement" && operation.source_journal_id === operation.target_journal_id) {
    return "补充复盘没有冻结独立的来源记录";
  }
  if (operation.state === "completed" && operation.result !== null) {
    if (operation.result.review_reference !== operation.review_reference
        || operation.result.source_journal_id !== operation.source_journal_id
        || operation.result.target_journal_id !== operation.target_journal_id
        || operation.outcome !== operation.result.outcome) {
      return "复盘结果与冻结的引用或目标身份不一致";
    }
    if (operation.result.outcome === "applied") {
      if (operation.result.derivation === null || operation.result.application === null
          || operation.result.derivation.application_id !== operation.result.application.id
          || operation.result.derivation.revision !== operation.result.target_revision) {
        return "复盘发布结果与派生身份或版本不一致";
      }
    } else if (operation.result.missing.length === 0
        || operation.result.derivation !== null
        || operation.result.application !== null) {
      return "复盘补问结果与缺失字段不一致";
    }
  }
  const appliedResult = operation.state === "completed"
    && operation.outcome === "applied"
    && operation.result !== null;
  const expectedBlock = !appliedResult
    ? "operation_not_applied"
    : operation.target_current_state !== "applied"
      ? "target_not_applied"
      : operation.target_current_revision !== operation.result?.target_revision
        ? "target_changed"
        : null;
  if (operation.undo_available !== (expectedBlock === null)
      || operation.undo_block_reason !== expectedBlock) {
    return "撤销可用性与目标复盘的当前状态不一致";
  }
  return null;
}
