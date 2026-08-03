import type {
  ApplicationNextAction,
  ApplicationStage,
} from "../applications/applicationContract";
import { normalizeApplicationIdentityPart } from "../applications/applicationIdentity.ts";

export type ApplicationUpdateOperationState = "completed" | "undone" | "stale";

export type ApplicationUpdateUndoBlockReason =
  | "target_missing"
  | "target_changed"
  | "prep_changed"
  | "provenance_changed"
  | "natural_key_taken"
  | "operation_invalid"
  | "already_undone";

export type ApplicationUpdateProjection = {
  company: string;
  company_id: number | null;
  position: string;
  stage: ApplicationStage;
  current_step: string | null;
  applied_date: string | null;
  next_action: ApplicationNextAction | null;
  paused_from_stage: ApplicationStage | null;
  pause_reason: string | null;
  application_note: string | null;
  jd_text: string | null;
  revision: number;
  application_updated_time: string;
};

export type ApplicationUpdateField =
  | "company"
  | "position"
  | "stage"
  | "current_step"
  | "next_action"
  | "application_note"
  | "jd_text";

export type ApplicationUpdateFieldChange =
  | { field: "company"; before: string; after: string }
  | { field: "position"; before: string; after: string }
  | { field: "stage"; before: ApplicationStage; after: ApplicationStage }
  | { field: "current_step"; before: string | null; after: string | null }
  | {
    field: "next_action";
    before: ApplicationNextAction | null;
    after: ApplicationNextAction | null;
  }
  | { field: "application_note"; before: string | null; after: string | null }
  | { field: "jd_text"; before: string | null; after: string | null };

export type ApplicationUpdateEffect = {
  changed_fields: ApplicationUpdateFieldChange[];
  question_provenance: {
    id: number;
    question_created_time: string;
    before_updated_time: string;
    after_updated_time: string;
    before_company: string | null;
    after_company: string;
  }[];
  question_occurrences: {
    journal_id: number;
    question_id: number;
    application_id: number;
    journal_created_time: string;
    journal_state: "applied";
    journal_revision: number;
    question_created_time: string;
    before_company: string;
    after_company: string;
    source_step: string | null;
    asked_date: string | null;
  }[];
  prep_invalidated: boolean;
  prep_restored_on_undo: false;
  company_record_created: boolean;
  company_records_retained_on_undo: true;
};

export type ApplicationUpdateCommandResult = {
  status: "ok";
  application_id: number;
  revision: number;
  timeline_entry_id: number | null;
  questions_updated: number;
  question_occurrences_updated: number;
  prep_invalidated: boolean;
};

export type ApplicationUpdateOperation = {
  operation_id: string;
  operation_type: "application_update";
  contract_version: 1;
  state: ApplicationUpdateOperationState;
  created_time: string;
  client_turn_id: string;
  target: {
    application_id: number;
    company: string;
    position: string;
    application_created_time: string;
  };
  before: ApplicationUpdateProjection;
  final: ApplicationUpdateProjection;
  effect: ApplicationUpdateEffect;
  result: {
    apply: ApplicationUpdateCommandResult;
    undo: ApplicationUpdateCommandResult | null;
  } | null;
  undo_available: boolean;
  undo_block_reason: ApplicationUpdateUndoBlockReason | null;
};

export type ApplicationUpdateUndoCommandStatus = {
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
      | "prep_changed"
      | "provenance_changed"
      | "natural_key_taken";
    message: string;
  } | null;
  finished_time: string | null;
};

export const STAGE_LABELS: Record<string, string> = {
  backlog: "待定",
  applied: "已投递",
  written_test: "笔试中",
  interviewing: "面试中",
  offer: "Offer",
  withdrawn: "不再跟进",
  rejected: "已挂",
  pooled: "泡池子",
};

export const FIELD_LABELS = {
  company: "公司",
  position: "岗位",
  stage: "阶段",
  current_step: "当前环节",
  next_action: "下一步",
  application_note: "岗位备注",
  jd_text: "职位描述",
} as const;

export const UNDO_BLOCK_LABELS: Record<ApplicationUpdateUndoBlockReason, string> = {
  target_missing: "岗位记录已不存在，不能安全撤销。",
  target_changed: "岗位后来又被修改，撤销会覆盖新改动，因此已被阻止。",
  prep_changed: "岗位后来生成或运行了新的 Prep；为避免清空后来产物，已阻止撤销。",
  provenance_changed: "关联题目的公司来源已经变化，不能按旧影响安全撤销。",
  natural_key_taken: "撤销后的公司与岗位组合已被另一条记录占用。",
  operation_invalid: "这份操作结果无法验证，暂时不能撤销。",
  already_undone: "这次修改已经撤销。",
};

const VALID_STAGES = new Set(Object.keys(STAGE_LABELS));
const VALID_STATES = new Set(["completed", "undone", "stale"]);
const UPDATE_FIELDS = [
  "company",
  "position",
  "stage",
  "current_step",
  "next_action",
  "application_note",
  "jd_text",
] as const satisfies readonly ApplicationUpdateField[];
const VALID_FIELDS = new Set<ApplicationUpdateField>(UPDATE_FIELDS);
const ACTIVE_STAGES = new Set<ApplicationStage>([
  "backlog", "applied", "written_test", "interviewing", "offer",
]);
const VALID_UNDO_BLOCK_REASONS = new Set(Object.keys(UNDO_BLOCK_LABELS));
const VALID_UNDO_COMMAND_STATES = new Set(["absent", "completed", "rejected"]);
const VALID_UNDO_COMMAND_ERROR_CODES = new Set([
  "operation_not_found",
  "operation_invalid",
  "target_missing",
  "target_changed",
  "prep_changed",
  "provenance_changed",
  "natural_key_taken",
]);
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
const ISO_DATE_PATTERN = /^\d{4}-\d{2}-\d{2}$/;
const CLOCK_TIME_PATTERN = /^(?:[01]\d|2[0-3]):[0-5]\d$/;

const OPERATION_KEYS = [
  "operation_id",
  "operation_type",
  "contract_version",
  "state",
  "created_time",
  "client_turn_id",
  "target",
  "before",
  "final",
  "effect",
  "result",
  "undo_available",
  "undo_block_reason",
] as const;
const TARGET_KEYS = [
  "application_id", "company", "position", "application_created_time",
] as const;
const PROJECTION_KEYS = [
  "company",
  "company_id",
  "position",
  "stage",
  "current_step",
  "applied_date",
  "next_action",
  "paused_from_stage",
  "pause_reason",
  "application_note",
  "jd_text",
  "revision",
  "application_updated_time",
] as const;
const NEXT_ACTION_KEYS = ["stage", "step", "date", "time", "note"] as const;
const EFFECT_KEYS = [
  "changed_fields",
  "question_provenance",
  "question_occurrences",
  "prep_invalidated",
  "prep_restored_on_undo",
  "company_record_created",
  "company_records_retained_on_undo",
] as const;
const FIELD_CHANGE_KEYS = ["field", "before", "after"] as const;
const QUESTION_PROVENANCE_KEYS = [
  "id",
  "question_created_time",
  "before_updated_time",
  "after_updated_time",
  "before_company",
  "after_company",
] as const;
const QUESTION_OCCURRENCE_KEYS = [
  "journal_id",
  "question_id",
  "application_id",
  "journal_created_time",
  "journal_state",
  "journal_revision",
  "question_created_time",
  "before_company",
  "after_company",
  "source_step",
  "asked_date",
] as const;
const RECEIPT_KEYS = ["apply", "undo"] as const;
const COMMAND_RESULT_KEYS = [
  "status",
  "application_id",
  "revision",
  "timeline_entry_id",
  "questions_updated",
  "question_occurrences_updated",
  "prep_invalidated",
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

function codePointLength(value: string): number {
  return Array.from(value).length;
}

function isTrimmedText(value: unknown, maxLength: number): value is string {
  return typeof value === "string"
    && value === value.trim()
    && codePointLength(value) >= 1
    && codePointLength(value) <= maxLength;
}

function isNullableTrimmedText(value: unknown, maxLength: number): value is string | null {
  return value === null || isTrimmedText(value, maxLength);
}

function isBoundedText(value: unknown, maxLength: number): value is string {
  return typeof value === "string"
    && codePointLength(value) >= 1
    && codePointLength(value) <= maxLength;
}

function isNullableLooseText(value: unknown, maxLength: number): value is string | null {
  return value === null
    || (typeof value === "string" && codePointLength(value) <= maxLength);
}

function isTimestamp(value: unknown): value is string {
  return isTrimmedText(value, 64);
}

function isNonNegativeInteger(value: unknown): value is number {
  return Number.isInteger(value) && (value as number) >= 0;
}

function isPositiveInteger(value: unknown): value is number {
  return Number.isInteger(value) && (value as number) > 0;
}

function isUuid(value: unknown): value is string {
  return typeof value === "string" && UUID_PATTERN.test(value);
}

function isStage(value: unknown): value is ApplicationStage {
  return typeof value === "string" && VALID_STAGES.has(value);
}

function isIsoDate(value: unknown): value is string {
  if (typeof value !== "string" || !ISO_DATE_PATTERN.test(value)) return false;
  const [year, month, day] = value.split("-").map(Number);
  const parsed = new Date(Date.UTC(year, month - 1, day));
  return parsed.getUTCFullYear() === year
    && parsed.getUTCMonth() === month - 1
    && parsed.getUTCDate() === day;
}

function isNextAction(value: unknown): value is ApplicationNextAction | null {
  if (value === null) return true;
  if (!isRecord(value)
      || !hasExactKeys(value, NEXT_ACTION_KEYS)
      || !isStage(value.stage)
      || !isTrimmedText(value.step, 300)
      || (value.date !== null && !isIsoDate(value.date))
      || (value.time !== null
        && (typeof value.time !== "string" || !CLOCK_TIME_PATTERN.test(value.time)))
      || !isNullableTrimmedText(value.note, 2_000)) return false;
  return value.time === null || value.date !== null;
}

function nextActionsEqual(
  left: ApplicationNextAction | null,
  right: ApplicationNextAction | null,
): boolean {
  if (left === null || right === null) return left === right;
  return left.stage === right.stage
    && left.step === right.step
    && left.date === right.date
    && left.time === right.time
    && left.note === right.note;
}

function isProjection(value: unknown): value is ApplicationUpdateProjection {
  if (!isRecord(value) || !hasExactKeys(value, PROJECTION_KEYS)) return false;
  return isTrimmedText(value.company, 200)
    && (value.company_id === null || isPositiveInteger(value.company_id))
    && isTrimmedText(value.position, 300)
    && isStage(value.stage)
    && isNullableTrimmedText(value.current_step, 300)
    && (value.applied_date === null || isIsoDate(value.applied_date))
    && isNextAction(value.next_action)
    && (value.paused_from_stage === null || isStage(value.paused_from_stage))
    && isNullableTrimmedText(value.pause_reason, 1_000)
    && isNullableTrimmedText(value.application_note, 2_000)
    && (value.jd_text === null || isBoundedText(value.jd_text, 50_000))
    && isNonNegativeInteger(value.revision)
    && isTimestamp(value.application_updated_time);
}

function isCommandResult(value: unknown): value is ApplicationUpdateCommandResult {
  if (!isRecord(value) || !hasExactKeys(value, COMMAND_RESULT_KEYS)) return false;
  return value.status === "ok"
    && isPositiveInteger(value.application_id)
    && isPositiveInteger(value.revision)
    && (value.timeline_entry_id === null || isPositiveInteger(value.timeline_entry_id))
    && isNonNegativeInteger(value.questions_updated)
    && value.questions_updated <= 100
    && isNonNegativeInteger(value.question_occurrences_updated)
    && value.question_occurrences_updated <= 1_000
    && typeof value.prep_invalidated === "boolean";
}

function isFieldChange(value: unknown): value is ApplicationUpdateFieldChange {
  if (!isRecord(value)
      || !hasExactKeys(value, FIELD_CHANGE_KEYS)
      || typeof value.field !== "string"
      || !VALID_FIELDS.has(value.field as ApplicationUpdateField)) return false;
  let valuesValid = false;
  let valuesEqual = false;
  switch (value.field) {
    case "company":
      valuesValid = isTrimmedText(value.before, 200) && isTrimmedText(value.after, 200);
      valuesEqual = value.before === value.after;
      break;
    case "position":
      valuesValid = isTrimmedText(value.before, 300) && isTrimmedText(value.after, 300);
      valuesEqual = value.before === value.after;
      break;
    case "stage":
      valuesValid = isStage(value.before) && isStage(value.after);
      valuesEqual = value.before === value.after;
      break;
    case "current_step":
      valuesValid = isNullableTrimmedText(value.before, 300)
        && isNullableTrimmedText(value.after, 300);
      valuesEqual = value.before === value.after;
      break;
    case "next_action":
      if (isNextAction(value.before) && isNextAction(value.after)) {
        valuesValid = true;
        valuesEqual = nextActionsEqual(value.before, value.after);
      }
      break;
    case "application_note":
      valuesValid = isNullableTrimmedText(value.before, 2_000)
        && isNullableTrimmedText(value.after, 2_000);
      valuesEqual = value.before === value.after;
      break;
    case "jd_text":
      valuesValid = (value.before === null || isBoundedText(value.before, 50_000))
        && (value.after === null || isBoundedText(value.after, 50_000));
      valuesEqual = value.before === value.after;
      break;
  }
  return valuesValid && !valuesEqual;
}

function isEffect(value: unknown): value is ApplicationUpdateEffect {
  if (!isRecord(value)
      || !hasExactKeys(value, EFFECT_KEYS)
      || !Array.isArray(value.changed_fields)
      || value.changed_fields.length < 1
      || value.changed_fields.length > 7
      || !Array.isArray(value.question_provenance)
      || value.question_provenance.length > 100
      || !Array.isArray(value.question_occurrences)
      || value.question_occurrences.length > 1_000
      || typeof value.prep_invalidated !== "boolean"
      || value.prep_restored_on_undo !== false
      || typeof value.company_record_created !== "boolean"
      || value.company_records_retained_on_undo !== true) return false;

  const fields = new Set<ApplicationUpdateField>();
  for (const item of value.changed_fields) {
    if (!isFieldChange(item) || fields.has(item.field)) return false;
    fields.add(item.field);
  }
  const questionIds = new Set<number>();
  for (const item of value.question_provenance) {
    if (!isRecord(item)
        || !hasExactKeys(item, QUESTION_PROVENANCE_KEYS)
        || !isPositiveInteger(item.id)
        || !isTimestamp(item.question_created_time)
        || !isTimestamp(item.before_updated_time)
        || !isTimestamp(item.after_updated_time)
        || !isNullableTrimmedText(item.before_company, 200)
        || !isTrimmedText(item.after_company, 200)
        || questionIds.has(item.id)) return false;
    questionIds.add(item.id);
  }
  const occurrenceIds = new Set<string>();
  for (const item of value.question_occurrences) {
    if (!isRecord(item)
        || !hasExactKeys(item, QUESTION_OCCURRENCE_KEYS)
        || !isPositiveInteger(item.journal_id)
        || !isPositiveInteger(item.question_id)
        || !isPositiveInteger(item.application_id)
        || !isTimestamp(item.journal_created_time)
        || item.journal_state !== "applied"
        || !isNonNegativeInteger(item.journal_revision)
        || !isTimestamp(item.question_created_time)
        || !isTrimmedText(item.before_company, 200)
        || !isTrimmedText(item.after_company, 200)
        || !isNullableLooseText(item.source_step, 300)
        || !isNullableLooseText(item.asked_date, 64)) return false;
    const key = `${item.journal_id}:${item.question_id}`;
    if (occurrenceIds.has(key)) return false;
    occurrenceIds.add(key);
  }
  return true;
}

export function isApplicationUpdateOperation(value: unknown): value is ApplicationUpdateOperation {
  if (!isRecord(value)
      || !hasExactKeys(value, OPERATION_KEYS)
      || !isUuid(value.operation_id)
      || value.operation_type !== "application_update"
      || value.contract_version !== 1
      || typeof value.state !== "string"
      || !VALID_STATES.has(value.state)
      || !isTimestamp(value.created_time)
      || !isUuid(value.client_turn_id)
      || !isRecord(value.target)
      || !hasExactKeys(value.target, TARGET_KEYS)
      || !isPositiveInteger(value.target.application_id)
      || !isTrimmedText(value.target.company, 200)
      || !isTrimmedText(value.target.position, 300)
      || !isTimestamp(value.target.application_created_time)
      || !isProjection(value.before)
      || !isProjection(value.final)
      || !isEffect(value.effect)
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

export function isApplicationUpdateUndoCommandStatus(
  value: unknown,
  expectedCommandId: string,
  expectedOperationId: string,
): value is ApplicationUpdateUndoCommandStatus {
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
      || !isTimestamp(value.finished_time)) return false;
  if (value.state === "completed") return value.error === null;
  return isRecord(value.error)
    && hasExactKeys(value.error, UNDO_COMMAND_ERROR_KEYS)
    && typeof value.error.code === "string"
    && VALID_UNDO_COMMAND_ERROR_CODES.has(value.error.code)
    && isTrimmedText(value.error.message, 256);
}

export function applicationUpdateOperationIntegrityIssue(
  operation: ApplicationUpdateOperation,
): string | null {
  if (operation.target.company !== operation.before.company
      || operation.target.position !== operation.before.position) {
    return "稳定目标身份与修改前快照不一致";
  }
  if (operation.before.revision + 1 !== operation.final.revision) {
    return "修改前后的版本号不连续";
  }
  const changed = new Map(operation.effect.changed_fields.map((item) => [item.field, item]));
  for (const field of UPDATE_FIELDS) {
    const effect = changed.get(field);
    if (effect) {
      const beforeMatches = field === "next_action"
        ? nextActionsEqual(operation.before.next_action, effect.before as ApplicationNextAction | null)
        : operation.before[field] === effect.before;
      const finalMatches = field === "next_action"
        ? nextActionsEqual(operation.final.next_action, effect.after as ApplicationNextAction | null)
        : operation.final[field] === effect.after;
      if (!beforeMatches || !finalMatches) {
        return `${FIELD_LABELS[field]}的字段影响与前后快照不一致`;
      }
    } else if (field === "next_action"
      ? !nextActionsEqual(operation.before.next_action, operation.final.next_action)
      : operation.before[field] !== operation.final[field]) {
      return `${FIELD_LABELS[field]}发生了未披露的变化`;
    }
  }
  const appliedDateInitialized = changed.has("stage")
    && operation.before.stage !== "applied"
    && operation.final.stage === "applied"
    && operation.before.applied_date === null;
  if (appliedDateInitialized
    ? operation.final.applied_date === null
    : operation.final.applied_date !== operation.before.applied_date) {
    return "投递日期与首次进入已投递阶段的规则不一致";
  }
  if (!changed.has("stage")) {
    if (operation.before.paused_from_stage !== operation.final.paused_from_stage
        || operation.before.pause_reason !== operation.final.pause_reason) {
      return "非阶段修改改变了暂停信息";
    }
  } else {
    const expectedPausedFrom = operation.final.stage === "pooled"
      && ACTIVE_STAGES.has(operation.before.stage)
      ? operation.before.stage
      : null;
    if (operation.final.paused_from_stage !== expectedPausedFrom
        || operation.final.pause_reason !== null) {
      return "阶段修改后的暂停信息与规则不一致";
    }
  }
  if ((operation.final.stage === "rejected" || operation.final.stage === "withdrawn")
      && operation.final.next_action !== null) {
    return "结束阶段仍保留了下一步";
  }
  const invalidatesPrep = changed.has("company")
    || changed.has("position")
    || changed.has("jd_text");
  if (operation.effect.prep_invalidated !== invalidatesPrep) {
    return "Prep 失效标记与身份或 JD 字段变化不一致";
  }
  if (!changed.has("company")
      && (operation.effect.question_provenance.length > 0
        || operation.effect.question_occurrences.length > 0
        || operation.effect.company_record_created)) {
    return "未修改公司却返回了公司来源连带影响";
  }
  if (!changed.has("company") && operation.before.company_id !== operation.final.company_id) {
    return "未修改公司却改变了公司身份 ID";
  }
  if (changed.has("company")) {
    const sameCompanyIdentity = normalizeApplicationIdentityPart(operation.before.company)
      === normalizeApplicationIdentityPart(operation.final.company);
    if (operation.final.company_id === null) {
      return "公司改名后缺少公司身份 ID";
    }
    if (sameCompanyIdentity
        && operation.before.company_id !== null
        && operation.final.company_id !== operation.before.company_id) {
      return "仅调整公司显示名却改变了公司身份 ID";
    }
    if (!sameCompanyIdentity
        && operation.final.company_id === operation.before.company_id) {
      return "公司自然键变化后没有正确改绑身份 ID";
    }
    if (operation.effect.company_record_created
        && operation.before.company_id !== null
        && operation.final.company_id === operation.before.company_id) {
      return "复用既有公司身份时错误标记为新建公司";
    }
  }
  if (changed.has("company") && (
    operation.effect.question_provenance.some((item) => (
      item.after_company !== operation.final.company
      || item.after_updated_time !== operation.final.application_updated_time
    ))
    || operation.effect.question_occurrences.some((item) => (
      item.application_id !== operation.target.application_id
      || item.after_company !== operation.final.company
    ))
  )) {
    return "公司来源连带影响与最终岗位身份不一致";
  }
  if (operation.result !== null) {
    const expectedTimelineEntry = changed.has("stage") || changed.has("current_step");
    const apply = operation.result.apply;
    if (apply.application_id !== operation.target.application_id
        || apply.revision !== operation.final.revision
        || (apply.timeline_entry_id !== null) !== expectedTimelineEntry
        || apply.questions_updated !== operation.effect.question_provenance.length
        || apply.question_occurrences_updated !== operation.effect.question_occurrences.length
        || apply.prep_invalidated !== operation.effect.prep_invalidated) {
      return "执行结果与冻结影响不一致";
    }
    const undo = operation.result.undo;
    if (undo && (undo.application_id !== operation.target.application_id
        || undo.revision !== operation.final.revision + 1
        || (undo.timeline_entry_id !== null) !== expectedTimelineEntry
        || undo.questions_updated !== operation.effect.question_provenance.length
        || undo.question_occurrences_updated !== operation.effect.question_occurrences.length
        || undo.prep_invalidated !== operation.effect.prep_invalidated)) {
      return "撤销结果与冻结影响不一致";
    }
  }
  return null;
}
