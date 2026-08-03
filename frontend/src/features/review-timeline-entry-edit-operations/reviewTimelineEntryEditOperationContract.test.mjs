import assert from "node:assert/strict";
import { test } from "node:test";

import {
  isReviewTimelineEntryEditOperation,
  isReviewTimelineEntryEditUndoCommandStatus,
  reviewTimelineEntryEditOperationIntegrityIssue,
} from "./reviewTimelineEntryEditOperationContract.ts";

const operationId = "00000000-0000-4000-8000-000000000001";
const turnId = "00000000-0000-4000-8000-000000000002";
const commandId = "00000000-0000-4000-8000-000000000003";

function completedOperation() {
  const apply = {
    status: "ok",
    journal_id: 7,
    timeline_entry_id: 11,
    application_id: 9,
    target_revision: 3,
    application_revision: 5,
    timeline_entries_updated: 1,
    occurrences_updated: 1,
    status_logs_updated: 1,
    application_updated: true,
  };
  return {
    operation_id: operationId,
    operation_type: "review_timeline_entry_edit",
    contract_version: 1,
    state: "completed",
    created_time: "2026-07-14T10:00:00Z",
    client_turn_id: turnId,
    request_digest: "c".repeat(64),
    target: {
      journal_id: 7,
      journal_created_time: "2026-07-13T10:00:00Z",
      application_id: 9,
      application_created_time: "2026-07-01T10:00:00Z",
      timeline_entry_id: 11,
      timeline_entry_created_time: "2026-07-13T10:00:00Z",
      company: "Example",
      position: "Engineer",
    },
    before: {
      step: "一面",
      occurred_date: "2026-07-13",
      outcome: "passed",
      summary: "技术面通过",
      from_stage: "interviewing",
      from_step: "一面",
      to_stage: "interviewing",
      to_step: "一面",
      journal_revision: 2,
    },
    final: {
      step: "二面",
      occurred_date: "2026-07-14",
      outcome: "passed",
      summary: "技术面通过",
      from_stage: "interviewing",
      from_step: "一面",
      to_stage: "interviewing",
      to_step: "二面",
      journal_revision: 3,
    },
    effect: {
      changed_fields: ["step", "occurred_date"],
      occurrences: [{
        question_id: 13,
        application_id: 9,
        company: "Example",
        before_source_step: "一面",
        after_source_step: "二面",
        before_asked_date: "2026-07-13",
        after_asked_date: "2026-07-14",
      }],
      status_logs: [{
        id: 17,
        created_time: "2026-07-13T10:00:00Z",
        before_log_date: "2026-07-13",
        after_log_date: "2026-07-14",
      }],
      application_before: {
        stage: "interviewing",
        current_step: "一面",
        current_state_entry_id: 11,
        revision: 4,
      },
      application_final: {
        stage: "interviewing",
        current_step: "二面",
        current_state_entry_id: 11,
        revision: 5,
      },
      before_dependency_fingerprint: "a".repeat(64),
      final_dependency_fingerprint: "b".repeat(64),
      questions_untouched: true,
      knowledge_untouched: true,
    },
    result: { apply, undo: null },
    undo_available: true,
    undo_block_reason: null,
  };
}

test("completed and undone timeline-entry edit receipts retain canonical state", () => {
  const completed = completedOperation();
  assert.equal(isReviewTimelineEntryEditOperation(completed), true);
  assert.equal(reviewTimelineEntryEditOperationIntegrityIssue(completed), null);

  const undone = structuredClone(completed);
  undone.state = "undone";
  undone.undo_available = false;
  undone.undo_block_reason = "already_undone";
  undone.result.undo = {
    ...undone.result.apply,
    target_revision: 4,
    application_revision: 6,
  };
  assert.equal(isReviewTimelineEntryEditOperation(undone), true);
  assert.equal(reviewTimelineEntryEditOperationIntegrityIssue(undone), null);
});

test("a structurally valid receipt with mismatched execution counts is flagged", () => {
  const corrupt = completedOperation();
  corrupt.result.apply.occurrences_updated = 0;
  assert.equal(isReviewTimelineEntryEditOperation(corrupt), true);
  assert.equal(
    reviewTimelineEntryEditOperationIntegrityIssue(corrupt),
    "复盘历程编辑结果与冻结影响不一致",
  );
});

test("legacy event and round projection keys are rejected", () => {
  const legacy = completedOperation();
  legacy.before.event_type = "interview";
  legacy.before.round = 2;
  assert.equal(isReviewTimelineEntryEditOperation(legacy), false);
});

test("operation validators fail closed on extra keys at every nested DTO boundary", () => {
  const corruptions = [
    (value) => { value.unexpected = true; },
    (value) => { value.target.unexpected = true; },
    (value) => { value.before.unexpected = true; },
    (value) => { value.final.unexpected = true; },
    (value) => { value.effect.unexpected = true; },
    (value) => { value.effect.occurrences[0].unexpected = true; },
    (value) => { value.effect.status_logs[0].unexpected = true; },
    (value) => { value.effect.application_before.unexpected = true; },
    (value) => { value.effect.application_final.unexpected = true; },
    (value) => { value.result.unexpected = true; },
    (value) => { value.result.apply.unexpected = true; },
  ];
  for (const corrupt of corruptions) {
    const operation = completedOperation();
    corrupt(operation);
    assert.equal(isReviewTimelineEntryEditOperation(operation), false);
  }
});

test("undo command status is bound to the expected command and operation IDs", () => {
  const absent = {
    command_id: commandId,
    operation_id: null,
    state: "absent",
    terminal: false,
    error: null,
    finished_time: null,
  };
  assert.equal(
    isReviewTimelineEntryEditUndoCommandStatus(absent, commandId, operationId),
    true,
  );

  const status = {
    command_id: commandId,
    operation_id: operationId,
    state: "completed",
    terminal: true,
    error: null,
    finished_time: "2026-07-14T10:01:00Z",
  };
  assert.equal(
    isReviewTimelineEntryEditUndoCommandStatus(status, commandId, operationId),
    true,
  );
  assert.equal(
    isReviewTimelineEntryEditUndoCommandStatus(status, turnId, operationId),
    false,
  );
  assert.equal(
    isReviewTimelineEntryEditUndoCommandStatus(status, commandId, turnId),
    false,
  );

  const rejected = {
    ...status,
    state: "rejected",
    error: { code: "target_changed", message: "复盘历程后来又被修改" },
  };
  assert.equal(
    isReviewTimelineEntryEditUndoCommandStatus(rejected, commandId, operationId),
    true,
  );

  assert.equal(
    isReviewTimelineEntryEditUndoCommandStatus(
      { ...status, unexpected: true },
      commandId,
      operationId,
    ),
    false,
  );
  assert.equal(
    isReviewTimelineEntryEditUndoCommandStatus(
      { ...rejected, error: { ...rejected.error, unexpected: true } },
      commandId,
      operationId,
    ),
    false,
  );
});
