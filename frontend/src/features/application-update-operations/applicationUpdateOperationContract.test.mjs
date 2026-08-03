import assert from "node:assert/strict";
import { test } from "node:test";

import {
  applicationUpdateOperationIntegrityIssue,
  isApplicationUpdateOperation,
  isApplicationUpdateUndoCommandStatus,
} from "./applicationUpdateOperationContract.ts";

const operationId = "00000000-0000-4000-8000-000000000001";
const turnId = "00000000-0000-4000-8000-000000000002";
const commandId = "00000000-0000-4000-8000-000000000003";

function commandResult(revision, timelineEntryId = null) {
  return {
    status: "ok",
    application_id: 7,
    revision,
    timeline_entry_id: timelineEntryId,
    questions_updated: 0,
    question_occurrences_updated: 0,
    prep_invalidated: true,
  };
}

function completedOperation(overrides = {}) {
  return {
    operation_id: operationId,
    operation_type: "application_update",
    contract_version: 1,
    state: "completed",
    created_time: "2026-07-14T10:00:00Z",
    client_turn_id: turnId,
    target: {
      application_id: 7,
      company: "Example",
      position: "Engineer",
      application_created_time: "2026-07-01T10:00:00Z",
    },
    before: {
      company: "Example",
      company_id: 11,
      position: "Engineer",
      stage: "backlog",
      current_step: "筛选岗位",
      applied_date: null,
      next_action: {
        stage: "applied",
        step: "提交申请",
        date: "2026-07-20",
        time: "09:30",
        note: "使用定制简历",
      },
      paused_from_stage: null,
      pause_reason: null,
      application_note: "优先处理",
      jd_text: null,
      revision: 2,
      application_updated_time: "2026-07-14T09:59:59Z",
    },
    final: {
      company: "Example",
      company_id: 11,
      position: "Staff Engineer",
      stage: "backlog",
      current_step: "筛选岗位",
      applied_date: null,
      next_action: {
        stage: "applied",
        step: "提交申请",
        date: "2026-07-20",
        time: "09:30",
        note: "使用定制简历",
      },
      paused_from_stage: null,
      pause_reason: null,
      application_note: "优先处理",
      jd_text: null,
      revision: 3,
      application_updated_time: "2026-07-14T10:00:00Z",
    },
    effect: {
      changed_fields: [
        { field: "position", before: "Engineer", after: "Staff Engineer" },
      ],
      question_provenance: [],
      question_occurrences: [],
      prep_invalidated: true,
      prep_restored_on_undo: false,
      company_record_created: false,
      company_records_retained_on_undo: true,
    },
    result: {
      apply: commandResult(3),
      undo: null,
    },
    undo_available: true,
    undo_block_reason: null,
    ...overrides,
  };
}

test("completed and undone application updates preserve their canonical receipts", () => {
  const completed = completedOperation();
  assert.equal(isApplicationUpdateOperation(completed), true);
  assert.equal(applicationUpdateOperationIntegrityIssue(completed), null);

  const undone = completedOperation({
    state: "undone",
    result: {
      apply: commandResult(3),
      undo: commandResult(4),
    },
    undo_available: false,
    undo_block_reason: "already_undone",
  });
  assert.equal(isApplicationUpdateOperation(undone), true);
  assert.equal(applicationUpdateOperationIntegrityIssue(undone), null);
});

test("a structurally valid receipt with inconsistent execution counts is quarantined", () => {
  const corrupt = completedOperation({
    result: {
      apply: commandResult(99),
      undo: null,
    },
  });
  assert.equal(isApplicationUpdateOperation(corrupt), true);
  assert.equal(
    applicationUpdateOperationIntegrityIssue(corrupt),
    "执行结果与冻结影响不一致",
  );
});

test("JD updates accept nullable before values and require Prep invalidation", () => {
  const base = completedOperation();
  const jdUpdate = completedOperation({
    before: { ...base.before, jd_text: null },
    final: { ...base.final, position: "Engineer", jd_text: "负责 FastAPI" },
    effect: {
      ...base.effect,
      changed_fields: [
        { field: "jd_text", before: null, after: "负责 FastAPI" },
      ],
      prep_invalidated: true,
    },
  });

  assert.equal(isApplicationUpdateOperation(jdUpdate), true);
  assert.equal(applicationUpdateOperationIntegrityIssue(jdUpdate), null);
  assert.equal(
    applicationUpdateOperationIntegrityIssue({
      ...jdUpdate,
      effect: { ...jdUpdate.effect, prep_invalidated: false },
    }),
    "Prep 失效标记与身份或 JD 字段变化不一致",
  );
});

test("current-step-only updates are accepted and require a timeline entry", () => {
  const base = completedOperation();
  const update = completedOperation({
    final: {
      ...base.final,
      position: "Engineer",
      current_step: "电话沟通",
    },
    effect: {
      ...base.effect,
      changed_fields: [
        { field: "current_step", before: "筛选岗位", after: "电话沟通" },
      ],
      prep_invalidated: false,
    },
    result: {
      apply: { ...commandResult(3, 41), prep_invalidated: false },
      undo: null,
    },
  });

  assert.equal(isApplicationUpdateOperation(update), true);
  assert.equal(applicationUpdateOperationIntegrityIssue(update), null);
  assert.equal(
    applicationUpdateOperationIntegrityIssue({
      ...update,
      result: {
        apply: { ...update.result.apply, timeline_entry_id: null },
        undo: null,
      },
    }),
    "执行结果与冻结影响不一致",
  );
});

test("first transition into applied initializes one real applied date", () => {
  const base = completedOperation();
  const update = completedOperation({
    final: {
      ...base.final,
      position: "Engineer",
      stage: "applied",
      applied_date: "2026-07-19",
    },
    effect: {
      ...base.effect,
      changed_fields: [{ field: "stage", before: "backlog", after: "applied" }],
      prep_invalidated: false,
    },
    result: {
      apply: { ...commandResult(3, 40), prep_invalidated: false },
      undo: null,
    },
  });

  assert.equal(isApplicationUpdateOperation(update), true);
  assert.equal(applicationUpdateOperationIntegrityIssue(update), null);
  const missingDate = structuredClone(update);
  missingDate.final.applied_date = null;
  assert.equal(
    applicationUpdateOperationIntegrityIssue(missingDate),
    "投递日期与首次进入已投递阶段的规则不一致",
  );
  const impossibleDate = structuredClone(base);
  impossibleDate.final.applied_date = "2026-07-19";
  assert.equal(
    applicationUpdateOperationIntegrityIssue(impossibleDate),
    "投递日期与首次进入已投递阶段的规则不一致",
  );
});

test("application-note updates are disclosed without requiring a timeline entry", () => {
  const base = completedOperation();
  const update = completedOperation({
    final: {
      ...base.final,
      position: "Engineer",
      application_note: "仅保留最终结论",
    },
    effect: {
      ...base.effect,
      changed_fields: [{
        field: "application_note", before: "优先处理", after: "仅保留最终结论",
      }],
      prep_invalidated: false,
    },
    result: {
      apply: { ...commandResult(3), prep_invalidated: false },
      undo: null,
    },
  });

  assert.equal(isApplicationUpdateOperation(update), true);
  assert.equal(applicationUpdateOperationIntegrityIssue(update), null);
});

test("display-only company rename keeps its normalized company identity", () => {
  const base = completedOperation();
  const update = completedOperation({
    before: {
      ...base.before,
      company: "Example Company",
    },
    target: {
      ...base.target,
      company: "Example Company",
    },
    final: {
      ...base.final,
      company: "ExampleCompany",
      position: "Engineer",
    },
    effect: {
      ...base.effect,
      changed_fields: [{
        field: "company", before: "Example Company", after: "ExampleCompany",
      }],
      prep_invalidated: true,
    },
  });

  assert.equal(isApplicationUpdateOperation(update), true);
  assert.equal(applicationUpdateOperationIntegrityIssue(update), null);
  assert.equal(
    applicationUpdateOperationIntegrityIssue({
      ...update,
      final: { ...update.final, company_id: 12 },
    }),
    "仅调整公司显示名却改变了公司身份 ID",
  );
  assert.equal(
    applicationUpdateOperationIntegrityIssue({
      ...update,
      effect: { ...update.effect, company_record_created: true },
    }),
    "复用既有公司身份时错误标记为新建公司",
  );
  assert.equal(
    applicationUpdateOperationIntegrityIssue({
      ...update,
      final: { ...update.final, company: "Other Company" },
      effect: {
        ...update.effect,
        changed_fields: [{
          field: "company", before: "Example Company", after: "Other Company",
        }],
      },
    }),
    "公司自然键变化后没有正确改绑身份 ID",
  );
});

test("next-action updates accept exact structured set and clear values without timeline writes", () => {
  const base = completedOperation();
  const action = {
    stage: "interviewing",
    step: "一面",
    date: "2026-07-22",
    time: "13:30",
    note: "准备项目案例",
  };
  const cleared = completedOperation({
    final: { ...base.final, position: "Engineer", next_action: null },
    effect: {
      ...base.effect,
      changed_fields: [
        { field: "next_action", before: base.before.next_action, after: null },
      ],
      prep_invalidated: false,
    },
    result: {
      apply: { ...commandResult(3), prep_invalidated: false },
      undo: null,
    },
  });
  const set = completedOperation({
    before: { ...base.before, next_action: null },
    final: { ...base.final, position: "Engineer", next_action: action },
    effect: {
      ...base.effect,
      changed_fields: [{ field: "next_action", before: null, after: action }],
      prep_invalidated: false,
    },
    result: {
      apply: { ...commandResult(3), prep_invalidated: false },
      undo: null,
    },
  });

  for (const operation of [cleared, set]) {
    assert.equal(isApplicationUpdateOperation(operation), true);
    assert.equal(applicationUpdateOperationIntegrityIssue(operation), null);
  }
});

test("combined stage and current-step updates preserve explicitly disclosed state", () => {
  const base = completedOperation();
  const update = completedOperation({
    final: {
      ...base.final,
      position: "Engineer",
      stage: "interviewing",
      current_step: "一面",
    },
    effect: {
      ...base.effect,
      changed_fields: [
        { field: "stage", before: "backlog", after: "interviewing" },
        { field: "current_step", before: "筛选岗位", after: "一面" },
      ],
      prep_invalidated: false,
    },
    result: {
      apply: { ...commandResult(3, 42), prep_invalidated: false },
      undo: null,
    },
  });

  assert.equal(isApplicationUpdateOperation(update), true);
  assert.equal(applicationUpdateOperationIntegrityIssue(update), null);
});

test("terminal stage updates disclose and clear an existing next action", () => {
  const base = completedOperation();
  const update = completedOperation({
    final: {
      ...base.final,
      position: "Engineer",
      stage: "rejected",
      next_action: null,
    },
    effect: {
      ...base.effect,
      changed_fields: [
        { field: "stage", before: "backlog", after: "rejected" },
        { field: "next_action", before: base.before.next_action, after: null },
      ],
      prep_invalidated: false,
    },
    result: {
      apply: { ...commandResult(3, 43), prep_invalidated: false },
      undo: null,
    },
  });

  assert.equal(isApplicationUpdateOperation(update), true);
  assert.equal(applicationUpdateOperationIntegrityIssue(update), null);
});

test("all seven update fields fit the canonical effect bound", () => {
  const base = completedOperation();
  const nextAction = {
    stage: "interviewing",
    step: "二面",
    date: "2026-07-24",
    time: null,
    note: null,
  };
  const update = completedOperation({
    final: {
      ...base.final,
      company: "Other",
      company_id: 12,
      stage: "interviewing",
      current_step: "一面",
      next_action: nextAction,
      application_note: "新备注",
      jd_text: "负责平台工程",
    },
    effect: {
      ...base.effect,
      changed_fields: [
        { field: "company", before: "Example", after: "Other" },
        { field: "position", before: "Engineer", after: "Staff Engineer" },
        { field: "stage", before: "backlog", after: "interviewing" },
        { field: "current_step", before: "筛选岗位", after: "一面" },
        { field: "next_action", before: base.before.next_action, after: nextAction },
        { field: "application_note", before: "优先处理", after: "新备注" },
        { field: "jd_text", before: null, after: "负责平台工程" },
      ],
      company_record_created: true,
    },
    result: {
      apply: commandResult(3, 44),
      undo: null,
    },
  });

  assert.equal(isApplicationUpdateOperation(update), true);
  assert.equal(applicationUpdateOperationIntegrityIssue(update), null);
});

test("operation validators fail closed on extra keys and field-specific value mismatches", () => {
  const base = completedOperation();
  const { jd_text: _omittedJd, ...projectionWithoutJd } = base.before;
  assert.equal(isApplicationUpdateOperation({ ...base, unexpected: true }), false);
  assert.equal(isApplicationUpdateOperation({
    ...base,
    before: projectionWithoutJd,
  }), false);
  assert.equal(isApplicationUpdateOperation({
    ...base,
    before: { ...base.before, unexpected: true },
  }), false);
  assert.equal(isApplicationUpdateOperation({
    ...base,
    result: {
      apply: { ...base.result.apply, unexpected: true },
      undo: null,
    },
  }), false);
  assert.equal(isApplicationUpdateOperation({
    ...base,
    effect: {
      ...base.effect,
      changed_fields: [
        { field: "current_step", before: "筛选岗位", after: base.before.next_action },
      ],
    },
  }), false);
  assert.equal(isApplicationUpdateOperation({
    ...base,
    effect: {
      ...base.effect,
      changed_fields: [{
        field: "next_action",
        before: base.before.next_action,
        after: { ...base.before.next_action, unexpected: true },
      }],
    },
  }), false);
  assert.equal(isApplicationUpdateOperation({
    ...base,
    effect: {
      ...base.effect,
      changed_fields: [{
        field: "next_action",
        before: base.before.next_action,
        after: { ...base.before.next_action, date: "2026-02-30" },
      }],
    },
  }), false);
});

test("undo command status is bound to both command and operation identity", () => {
  const absent = {
    command_id: commandId,
    operation_id: null,
    state: "absent",
    terminal: false,
    error: null,
    finished_time: null,
  };
  assert.equal(
    isApplicationUpdateUndoCommandStatus(absent, commandId, operationId),
    true,
  );

  const completed = {
    command_id: commandId,
    operation_id: operationId,
    state: "completed",
    terminal: true,
    error: null,
    finished_time: "2026-07-14T10:01:00Z",
  };
  assert.equal(
    isApplicationUpdateUndoCommandStatus(completed, commandId, operationId),
    true,
  );
  assert.equal(
    isApplicationUpdateUndoCommandStatus(completed, commandId, turnId),
    false,
  );
  assert.equal(
    isApplicationUpdateUndoCommandStatus(
      { ...completed, unexpected: true },
      commandId,
      operationId,
    ),
    false,
  );

  const rejected = {
    ...completed,
    state: "rejected",
    error: { code: "target_changed", message: "岗位后来又被修改" },
  };
  assert.equal(
    isApplicationUpdateUndoCommandStatus(rejected, commandId, operationId),
    true,
  );
  assert.equal(
    isApplicationUpdateUndoCommandStatus(
      { ...rejected, error: { code: "unexpected", message: "bad" } },
      commandId,
      operationId,
    ),
    false,
  );
  assert.equal(
    isApplicationUpdateUndoCommandStatus(
      { ...rejected, error: { ...rejected.error, unexpected: true } },
      commandId,
      operationId,
    ),
    false,
  );
});
