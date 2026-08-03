import type { ApplicationStage } from "../applications/applicationContract";

export type ReviewTimelineEntryEditField =
  | "step"
  | "occurred_date"
  | "outcome"
  | "summary";

export type ReviewTimelineEntryEditUndoBlockReason =
  | "target_missing"
  | "target_changed"
  | "provenance_changed"
  | "operation_invalid"
  | "already_undone";

export type ReviewTimelineEntryProjection = {
  step: string | null;
  occurred_date: string | null;
  outcome: "passed" | "failed" | "cancelled" | null;
  summary: string | null;
  from_stage: ApplicationStage;
  from_step: string | null;
  to_stage: ApplicationStage;
  to_step: string | null;
  journal_revision: number;
};

export type ReviewTimelineEntryEditApplicationProjection = {
  stage: ApplicationStage;
  current_step: string | null;
  current_state_entry_id: number | null;
  revision: number;
};

export type ReviewTimelineEntryEditEffect = {
  changed_fields: ReviewTimelineEntryEditField[];
  occurrences: {
    question_id: number;
    application_id: number;
    company: string;
    before_source_step: string | null;
    after_source_step: string | null;
    before_asked_date: string | null;
    after_asked_date: string | null;
  }[];
  status_logs: {
    id: number;
    created_time: string;
    before_log_date: string;
    after_log_date: string;
  }[];
  application_before: ReviewTimelineEntryEditApplicationProjection;
  application_final: ReviewTimelineEntryEditApplicationProjection;
  before_dependency_fingerprint: string;
  final_dependency_fingerprint: string;
  questions_untouched: true;
  knowledge_untouched: true;
};

export type ReviewTimelineEntryEditCommandResult = {
  status: "ok";
  journal_id: number;
  timeline_entry_id: number;
  application_id: number;
  target_revision: number;
  application_revision: number;
  timeline_entries_updated: 1;
  occurrences_updated: number;
  status_logs_updated: number;
  application_updated: true;
};

export type ReviewTimelineEntryEditOperation = {
  operation_id: string;
  operation_type: "review_timeline_entry_edit";
  contract_version: 1;
  state: "completed" | "undone" | "stale";
  created_time: string;
  client_turn_id: string;
  request_digest: string;
  target: {
    journal_id: number;
    journal_created_time: string;
    application_id: number;
    application_created_time: string;
    timeline_entry_id: number;
    timeline_entry_created_time: string;
    company: string;
    position: string;
  };
  before: ReviewTimelineEntryProjection;
  final: ReviewTimelineEntryProjection;
  effect: ReviewTimelineEntryEditEffect;
  result: {
    apply: ReviewTimelineEntryEditCommandResult;
    undo: ReviewTimelineEntryEditCommandResult | null;
  } | null;
  undo_available: boolean;
  undo_block_reason: ReviewTimelineEntryEditUndoBlockReason | null;
};

export type ReviewTimelineEntryEditUndoCommandStatus = {
  command_id: string;
  operation_id: string | null;
  state: "absent" | "completed" | "rejected";
  terminal: boolean;
  error: {
    code:
      | "operation_not_found"
      | "operation_invalid"
      | "target_missing"
      | "target_changed"
      | "provenance_changed";
    message: string;
  } | null;
  finished_time: string | null;
};

const VALID_STAGES = new Set([
  "backlog",
  "applied",
  "written_test",
  "interviewing",
  "offer",
  "withdrawn",
  "rejected",
  "pooled",
]);
const VALID_STATES = new Set(["completed", "undone", "stale"]);
const VALID_OUTCOMES = new Set(["passed", "failed", "cancelled"]);
const VALID_EDIT_FIELDS = new Set(["step", "occurred_date", "outcome", "summary"]);
const VALID_UNDO_BLOCK_REASONS = new Set([
  "target_missing",
  "target_changed",
  "provenance_changed",
  "operation_invalid",
  "already_undone",
]);
const VALID_UNDO_COMMAND_STATES = new Set(["absent", "completed", "rejected"]);
const VALID_UNDO_ERROR_CODES = new Set([
  "operation_not_found",
  "operation_invalid",
  "target_missing",
  "target_changed",
  "provenance_changed",
]);
export const REVIEW_TIMELINE_ENTRY_EDIT_FIELD_LABELS = {
  step: "具体环节",
  occurred_date: "发生日期",
  outcome: "结果",
  summary: "说明",
} as const;
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const DIGEST_PATTERN = /^[0-9a-f]{64}$/;
const OPERATION_KEYS = [
  "operation_id", "operation_type", "contract_version", "state", "created_time",
  "client_turn_id", "request_digest", "target", "before", "final", "effect",
  "result", "undo_available", "undo_block_reason",
] as const;
const TARGET_KEYS = [
  "journal_id", "journal_created_time", "application_id", "application_created_time",
  "timeline_entry_id", "timeline_entry_created_time", "company", "position",
] as const;
const TIMELINE_ENTRY_PROJECTION_KEYS = [
  "step", "occurred_date", "outcome", "summary", "from_stage", "from_step",
  "to_stage", "to_step", "journal_revision",
] as const;
const APPLICATION_PROJECTION_KEYS = [
  "stage", "current_step", "current_state_entry_id", "revision",
] as const;
const EFFECT_KEYS = [
  "changed_fields", "occurrences", "status_logs", "application_before",
  "application_final", "before_dependency_fingerprint", "final_dependency_fingerprint",
  "questions_untouched", "knowledge_untouched",
] as const;
const OCCURRENCE_KEYS = [
  "question_id", "application_id", "company", "before_source_step", "after_source_step",
  "before_asked_date", "after_asked_date",
] as const;
const STATUS_LOG_KEYS = ["id", "created_time", "before_log_date", "after_log_date"] as const;
const RECEIPT_KEYS = ["apply", "undo"] as const;
const COMMAND_RESULT_KEYS = [
  "status", "journal_id", "timeline_entry_id", "application_id", "target_revision",
  "application_revision", "timeline_entries_updated", "occurrences_updated",
  "status_logs_updated", "application_updated",
] as const;
const UNDO_COMMAND_STATUS_KEYS = [
  "command_id", "operation_id", "state", "terminal", "error", "finished_time",
] as const;
const UNDO_COMMAND_ERROR_KEYS = ["code", "message"] as const;

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function hasExactKeys(
  value: Record<string, unknown>,
  expected: readonly string[],
): boolean {
  const actual = Object.keys(value);
  return actual.length === expected.length
    && expected.every((key) => Object.hasOwn(value, key));
}

function isNonEmptyString(value: unknown): value is string {
  return typeof value === "string" && value.trim().length > 0;
}

function isOptionalNonEmptyString(value: unknown): value is string | null {
  return value === null || isNonEmptyString(value);
}

function isNonNegativeInteger(value: unknown): value is number {
  return Number.isInteger(value) && (value as number) >= 0;
}

function isPositiveInteger(value: unknown): value is number {
  return Number.isInteger(value) && (value as number) > 0;
}

function isOptionalPositiveInteger(value: unknown): value is number | null {
  return value === null || isPositiveInteger(value);
}

function isUuid(value: unknown): value is string {
  return typeof value === "string" && UUID_PATTERN.test(value);
}

function isStage(value: unknown): value is ApplicationStage {
  return typeof value === "string" && VALID_STAGES.has(value);
}

function isIsoDate(value: unknown): value is string {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return false;
  const [year, month, day] = value.split("-").map(Number);
  const parsed = new Date(Date.UTC(year, month - 1, day));
  return parsed.getUTCFullYear() === year
    && parsed.getUTCMonth() === month - 1
    && parsed.getUTCDate() === day;
}

function isOptionalIsoDate(value: unknown): value is string | null {
  return value === null || isIsoDate(value);
}

function isTimelineEntryProjection(value: unknown): value is ReviewTimelineEntryProjection {
  if (!isRecord(value) || !hasExactKeys(value, TIMELINE_ENTRY_PROJECTION_KEYS)) return false;
  return isOptionalNonEmptyString(value.step)
    && isOptionalIsoDate(value.occurred_date)
    && (value.outcome === null
      || (typeof value.outcome === "string" && VALID_OUTCOMES.has(value.outcome)))
    && isOptionalNonEmptyString(value.summary)
    && isStage(value.from_stage)
    && isOptionalNonEmptyString(value.from_step)
    && isStage(value.to_stage)
    && isOptionalNonEmptyString(value.to_step)
    && isNonNegativeInteger(value.journal_revision);
}

function isApplicationProjection(
  value: unknown,
): value is ReviewTimelineEntryEditApplicationProjection {
  if (!isRecord(value) || !hasExactKeys(value, APPLICATION_PROJECTION_KEYS)) return false;
  return isStage(value.stage)
    && isOptionalNonEmptyString(value.current_step)
    && isOptionalPositiveInteger(value.current_state_entry_id)
    && isNonNegativeInteger(value.revision);
}

function isEditEffect(value: unknown): value is ReviewTimelineEntryEditEffect {
  if (!isRecord(value)
      || !hasExactKeys(value, EFFECT_KEYS)
      || !Array.isArray(value.changed_fields)
      || value.changed_fields.length < 1
      || value.changed_fields.length > 4
      || !Array.isArray(value.occurrences)
      || value.occurrences.length > 50
      || !Array.isArray(value.status_logs)
      || value.status_logs.length > 1
      || !isApplicationProjection(value.application_before)
      || !isApplicationProjection(value.application_final)
      || typeof value.before_dependency_fingerprint !== "string"
      || !DIGEST_PATTERN.test(value.before_dependency_fingerprint)
      || typeof value.final_dependency_fingerprint !== "string"
      || !DIGEST_PATTERN.test(value.final_dependency_fingerprint)
      || value.questions_untouched !== true
      || value.knowledge_untouched !== true) return false;

  const fields = new Set<string>();
  for (const field of value.changed_fields) {
    if (typeof field !== "string" || !VALID_EDIT_FIELDS.has(field) || fields.has(field)) {
      return false;
    }
    fields.add(field);
  }
  if (!fields.has("occurred_date") && value.status_logs.length > 0) return false;
  if (!fields.has("step") && !fields.has("occurred_date") && value.occurrences.length > 0) {
    return false;
  }

  const questionIds = new Set<number>();
  for (const item of value.occurrences) {
    if (!isRecord(item)
        || !hasExactKeys(item, OCCURRENCE_KEYS)
        || !isPositiveInteger(item.question_id)
        || !isPositiveInteger(item.application_id)
        || !isNonEmptyString(item.company)
        || !isOptionalNonEmptyString(item.before_source_step)
        || !isOptionalNonEmptyString(item.after_source_step)
        || !isOptionalIsoDate(item.before_asked_date)
        || !isOptionalIsoDate(item.after_asked_date)
        || (item.before_source_step === item.after_source_step
          && item.before_asked_date === item.after_asked_date)
        || questionIds.has(item.question_id)) return false;
    questionIds.add(item.question_id);
  }
  for (const item of value.status_logs) {
    if (!isRecord(item)
        || !hasExactKeys(item, STATUS_LOG_KEYS)
        || !isPositiveInteger(item.id)
        || !isNonEmptyString(item.created_time)
        || !isIsoDate(item.before_log_date)
        || !isIsoDate(item.after_log_date)
        || item.before_log_date === item.after_log_date) return false;
  }

  const before = value.application_before;
  const final = value.application_final;
  return final.revision === before.revision + 1
    && final.stage === before.stage
    && final.current_state_entry_id === before.current_state_entry_id;
}

function isCommandResult(value: unknown): value is ReviewTimelineEntryEditCommandResult {
  if (!isRecord(value) || !hasExactKeys(value, COMMAND_RESULT_KEYS)) return false;
  return value.status === "ok"
    && isPositiveInteger(value.journal_id)
    && isPositiveInteger(value.timeline_entry_id)
    && isPositiveInteger(value.application_id)
    && isPositiveInteger(value.target_revision)
    && isPositiveInteger(value.application_revision)
    && value.timeline_entries_updated === 1
    && isNonNegativeInteger(value.occurrences_updated)
    && value.occurrences_updated <= 50
    && isNonNegativeInteger(value.status_logs_updated)
    && value.status_logs_updated <= 1
    && value.application_updated === true;
}

export function isReviewTimelineEntryEditOperation(
  value: unknown,
): value is ReviewTimelineEntryEditOperation {
  if (!isRecord(value)
      || !hasExactKeys(value, OPERATION_KEYS)
      || !isUuid(value.operation_id)
      || value.operation_type !== "review_timeline_entry_edit"
      || value.contract_version !== 1
      || typeof value.state !== "string"
      || !VALID_STATES.has(value.state)
      || !isNonEmptyString(value.created_time)
      || !isUuid(value.client_turn_id)
      || typeof value.request_digest !== "string"
      || !DIGEST_PATTERN.test(value.request_digest)
      || !isRecord(value.target)
      || !hasExactKeys(value.target, TARGET_KEYS)
      || !isPositiveInteger(value.target.journal_id)
      || !isNonEmptyString(value.target.journal_created_time)
      || !isPositiveInteger(value.target.application_id)
      || !isNonEmptyString(value.target.application_created_time)
      || !isPositiveInteger(value.target.timeline_entry_id)
      || !isNonEmptyString(value.target.timeline_entry_created_time)
      || !isNonEmptyString(value.target.company)
      || !isNonEmptyString(value.target.position)
      || !isTimelineEntryProjection(value.before)
      || !isTimelineEntryProjection(value.final)
      || !isEditEffect(value.effect)
      || typeof value.undo_available !== "boolean"
      || (value.undo_block_reason !== null
        && (typeof value.undo_block_reason !== "string"
          || !VALID_UNDO_BLOCK_REASONS.has(value.undo_block_reason)))) return false;
  if (value.result !== null && (!isRecord(value.result)
      || !hasExactKeys(value.result, RECEIPT_KEYS)
      || !isCommandResult(value.result.apply)
      || (value.result.undo !== null && !isCommandResult(value.result.undo)))) return false;
  if (value.state === "completed") {
    return value.result !== null
      && value.result.undo === null
      && (value.undo_available ? value.undo_block_reason === null : value.undo_block_reason !== null);
  }
  if (value.state === "undone") {
    return value.result !== null
      && value.result.undo !== null
      && !value.undo_available
      && value.undo_block_reason === "already_undone";
  }
  return value.result === null
    && !value.undo_available
    && value.undo_block_reason === "operation_invalid";
}

export function isReviewTimelineEntryEditUndoCommandStatus(
  value: unknown,
  expectedCommandId: string,
  expectedOperationId: string,
): value is ReviewTimelineEntryEditUndoCommandStatus {
  if (!isRecord(value)
      || !hasExactKeys(value, UNDO_COMMAND_STATUS_KEYS)
      || value.command_id !== expectedCommandId
      || !isUuid(value.command_id)
      || typeof value.state !== "string"
      || !VALID_UNDO_COMMAND_STATES.has(value.state)
      || typeof value.terminal !== "boolean") return false;
  if (value.state === "absent") {
    return value.terminal === false
      && value.operation_id === null
      && value.error === null
      && value.finished_time === null;
  }
  if (value.terminal !== true
      || value.operation_id !== expectedOperationId
      || !isUuid(value.operation_id)
      || !isNonEmptyString(value.finished_time)) return false;
  if (value.state === "completed") return value.error === null;
  return isRecord(value.error)
    && hasExactKeys(value.error, UNDO_COMMAND_ERROR_KEYS)
    && typeof value.error.code === "string"
    && VALID_UNDO_ERROR_CODES.has(value.error.code)
    && isNonEmptyString(value.error.message);
}

export function reviewTimelineEntryEditOperationIntegrityIssue(
  operation: ReviewTimelineEntryEditOperation,
): string | null {
  if (operation.final.journal_revision !== operation.before.journal_revision + 1) {
    return "复盘修订版本不连续";
  }
  const changed = new Set(operation.effect.changed_fields);
  for (const field of ["step", "occurred_date", "outcome", "summary"] as const) {
    const didChange = operation.before[field] !== operation.final[field];
    if (didChange !== changed.has(field)) {
      return `${REVIEW_TIMELINE_ENTRY_EDIT_FIELD_LABELS[field]}的影响与前后快照不一致`;
    }
  }
  if (operation.effect.occurrences.some((item) => (
    item.application_id !== operation.target.application_id
    || item.company !== operation.target.company
    || (changed.has("step") && (
      item.before_source_step !== operation.before.step
      || item.after_source_step !== operation.final.step
    ))
    || (changed.has("occurred_date") && (
      item.before_asked_date !== operation.before.occurred_date
      || item.after_asked_date !== operation.final.occurred_date
    ))
  ))) return "真题出处连带影响与历程快照不一致";
  if (operation.effect.status_logs.some((item) => (
    operation.final.occurred_date === null
    || item.after_log_date !== operation.final.occurred_date
    || (operation.before.occurred_date !== null
      && item.before_log_date !== operation.before.occurred_date)
  ))) return "状态日志日期影响与历程快照不一致";

  const applicationBefore = operation.effect.application_before;
  const applicationFinal = operation.effect.application_final;
  if (applicationFinal.revision !== applicationBefore.revision + 1) {
    return "岗位投影与修订版本不一致";
  }
  if (operation.result !== null) {
    const apply = operation.result.apply;
    if (apply.journal_id !== operation.target.journal_id
        || apply.timeline_entry_id !== operation.target.timeline_entry_id
        || apply.application_id !== operation.target.application_id
        || apply.target_revision !== operation.final.journal_revision
        || apply.application_revision !== applicationFinal.revision
        || apply.timeline_entries_updated !== 1
        || apply.occurrences_updated !== operation.effect.occurrences.length
        || apply.status_logs_updated !== operation.effect.status_logs.length
        || apply.application_updated !== true) {
      return "复盘历程编辑结果与冻结影响不一致";
    }
    const undo = operation.result.undo;
    if (undo && (undo.journal_id !== operation.target.journal_id
        || undo.timeline_entry_id !== operation.target.timeline_entry_id
        || undo.application_id !== operation.target.application_id
        || undo.target_revision !== operation.final.journal_revision + 1
        || undo.application_revision !== applicationFinal.revision + 1
        || undo.timeline_entries_updated !== 1
        || undo.occurrences_updated !== operation.effect.occurrences.length
        || undo.status_logs_updated !== operation.effect.status_logs.length
        || undo.application_updated !== true)) {
      return "复盘历程撤销结果与冻结影响不一致";
    }
  }
  return null;
}
