import assert from "node:assert/strict";
import { test } from "node:test";

import {
  isReviewRecordOperation,
  reviewRecordIntegrityIssue,
} from "./reviewRecordOperationContract.ts";

const operationId = "00000000-0000-4000-8000-000000000001";
const turnId = "00000000-0000-4000-8000-000000000002";

function appliedOperation(overrides = {}) {
  const extraction = {
    company: "Example",
    position: "Engineer",
    channel: null,
    history: { step: "一面", date: "2026-07-14", outcome: null, summary: "完成一面" },
    projected_state: { stage: "interviewing", current_step: "一面" },
    clear_next_action: false,
    next_action: null,
    questions: [],
    mood: null,
    time_of_day: null,
    factors: [],
  };
  const result = {
    outcome: "applied",
    review_reference: operationId,
    source_journal_id: 7,
    target_journal_id: 7,
    target_revision: 1,
    extraction,
    missing: [],
    derivation: {
      application_id: 9,
      application_created: false,
      timeline_entry_ids: [11],
      question_ids: [],
      knowledge_point_ids: [],
      status_log_ids: [],
      application_before: {
        stage: "applied",
        current_step: "简历筛选",
        current_state_entry_id: 10,
        next_action: null,
        paused_from_stage: null,
        pause_reason: null,
        channel: null,
        applied_date: "2026-07-01",
        revision: 3,
      },
      application_after: {
        stage: "interviewing",
        current_step: "一面",
        current_state_entry_id: 11,
        next_action: null,
        paused_from_stage: null,
        pause_reason: null,
        channel: null,
        applied_date: "2026-07-01",
        revision: 4,
      },
      revision: 1,
    },
    application: { id: 9, company: "Example", position: "Engineer" },
  };
  return {
    operation_type: "review_record",
    contract_version: 1,
    operation_id: operationId,
    review_reference: operationId,
    client_turn_id: turnId,
    mode: "initial",
    state: "completed",
    terminal: true,
    outcome: "applied",
    created_time: "2026-07-14T10:00:00Z",
    finished_time: "2026-07-14T10:00:01Z",
    source_journal_id: 7,
    target_journal_id: 7,
    target_current_state: "applied",
    target_current_revision: 1,
    preview: null,
    result,
    error: null,
    undo_available: true,
    undo_block_reason: null,
    ...overrides,
  };
}

function pendingOperation({ extraction = {}, missing = [], targetPlan } = {}) {
  const applied = appliedOperation();
  const previewExtraction = { ...applied.result.extraction, ...extraction };
  const resolvedTargetPlan = targetPlan === undefined
    ? previewExtraction.company === null || previewExtraction.position === null
      ? null
      : {
          kind: "new",
          company: previewExtraction.company,
          position: previewExtraction.position,
          current_stage: "backlog",
          current_step: null,
          projected_stage: "interviewing",
          projected_step: "一面",
          current_next_action: null,
          projected_next_action: null,
          current_applied_date: null,
          projected_applied_date: null,
          current_channel: null,
          projected_channel: previewExtraction.channel,
        }
    : targetPlan;
  return {
    ...applied,
    state: "pending_confirmation",
    terminal: false,
    outcome: null,
    finished_time: null,
    target_current_state: "pending",
    target_current_revision: 0,
    preview: {
      extraction: previewExtraction,
      target_plan: resolvedTargetPlan,
      missing,
    },
    result: null,
    error: null,
    undo_available: false,
    undo_block_reason: "operation_not_applied",
  };
}

test("an unchanged applied record exposes a canonical undo entry", () => {
  const operation = appliedOperation();
  assert.equal(isReviewRecordOperation(operation), true);
  assert.equal(reviewRecordIntegrityIssue(operation), null);
});

test("a voided target remains a valid historical receipt but blocks undo", () => {
  const operation = appliedOperation({
    target_current_state: "voided",
    target_current_revision: 2,
    undo_available: false,
    undo_block_reason: "target_not_applied",
  });
  assert.equal(isReviewRecordOperation(operation), true);
  assert.equal(reviewRecordIntegrityIssue(operation), null);
});

test("dynamic undo flags that disagree with the target are rejected", () => {
  const operation = appliedOperation({
    target_current_state: "voided",
    target_current_revision: 2,
  });
  assert.equal(isReviewRecordOperation(operation), false);
});

test("strict record receipts reject unknown top-level fields", () => {
  assert.equal(isReviewRecordOperation(appliedOperation({ raw_text: "secret" })), false);
});

test("completed receipts require exact before and after application snapshots", () => {
  const operation = appliedOperation();
  assert.equal(isReviewRecordOperation(operation), true);

  const missingBefore = structuredClone(operation);
  delete missingBefore.result.derivation.application_before;
  assert.equal(isReviewRecordOperation(missingBefore), false);

  const extraAfterField = structuredClone(operation);
  extraAfterField.result.derivation.application_after.legacy_status = "interviewing";
  assert.equal(isReviewRecordOperation(extraAfterField), false);

  const invalidBeforeEntry = structuredClone(operation);
  invalidBeforeEntry.result.derivation.application_before.current_state_entry_id = 0;
  assert.equal(isReviewRecordOperation(invalidBeforeEntry), false);

  const invalidAfterDate = structuredClone(operation);
  invalidAfterDate.result.derivation.application_after.applied_date = "2026-02-30";
  assert.equal(isReviewRecordOperation(invalidAfterDate), false);

  const mismatchedStep = structuredClone(operation);
  mismatchedStep.result.derivation.application_after.current_step = "二面";
  assert.equal(isReviewRecordOperation(mismatchedStep), false);

  const mismatchedPlan = structuredClone(operation);
  mismatchedPlan.result.derivation.application_after.next_action = {
    stage: "interviewing", step: "二面", date: null, time: null, note: null,
  };
  assert.equal(isReviewRecordOperation(mismatchedPlan), false);

  const mismatchedChannel = structuredClone(operation);
  mismatchedChannel.result.derivation.application_after.channel = "内推";
  assert.equal(isReviewRecordOperation(mismatchedChannel), false);

  const mismatchedIdentity = structuredClone(operation);
  mismatchedIdentity.result.application.company = "Other";
  assert.equal(isReviewRecordOperation(mismatchedIdentity), false);

  const terminalWithPlan = structuredClone(operation);
  terminalWithPlan.result.derivation.application_after.stage = "rejected";
  terminalWithPlan.result.derivation.application_after.next_action = {
    stage: "interviewing", step: "二面", date: null, time: null, note: null,
  };
  assert.equal(isReviewRecordOperation(terminalWithPlan), false);

  const nonPooledPause = structuredClone(operation);
  nonPooledPause.result.derivation.application_after.paused_from_stage = "applied";
  assert.equal(isReviewRecordOperation(nonPooledPause), false);
});

test("pending confirmation accepts complete and hard-identity-missing previews", () => {
  assert.equal(isReviewRecordOperation(pendingOperation()), true);
  assert.equal(isReviewRecordOperation(pendingOperation({
    extraction: { company: null, position: null },
    missing: [{ field: "company", ask: "这场是哪家公司的？" }],
  })), true);
  assert.equal(isReviewRecordOperation(pendingOperation({
    targetPlan: {
      kind: "existing",
      application_id: 9,
      company: "Example",
      position: "Engineer",
      created_time: "2026-07-01T10:00:00Z",
      revision: 0,
      current_stage: "applied",
      current_step: null,
      projected_stage: "interviewing",
      projected_step: "一面",
      current_next_action: null,
      projected_next_action: null,
      current_applied_date: "2026-07-01",
      projected_applied_date: "2026-07-01",
      current_channel: null,
      projected_channel: null,
    },
  })), true);
});

test("applied date is inferred only by an actual transition to applied", () => {
  const existingAppliedPlan = {
    ...pendingOperation().preview.target_plan,
    kind: "existing",
    application_id: 9,
    created_time: "2026-07-01T10:00:00Z",
    revision: 3,
    current_stage: "applied",
    current_step: "已提交",
    projected_stage: "applied",
    projected_step: "已提交",
    current_applied_date: null,
    projected_applied_date: null,
  };
  const ordinaryReview = pendingOperation({
    extraction: { projected_state: null },
    targetPlan: existingAppliedPlan,
  });
  assert.equal(isReviewRecordOperation(ordinaryReview), true);

  const inferredWithoutExplicitProjection = structuredClone(ordinaryReview);
  inferredWithoutExplicitProjection.preview.target_plan.projected_applied_date = "2026-07-14";
  assert.equal(isReviewRecordOperation(inferredWithoutExplicitProjection), false);

  const explicitProjection = structuredClone(ordinaryReview);
  explicitProjection.preview.extraction.projected_state = {
    stage: "applied", current_step: null,
  };
  explicitProjection.preview.target_plan.projected_applied_date = "2026-07-14";
  assert.equal(isReviewRecordOperation(explicitProjection), false);

  const actualTransition = structuredClone(ordinaryReview);
  actualTransition.preview.target_plan.current_stage = "backlog";
  actualTransition.preview.target_plan.current_step = null;
  actualTransition.preview.extraction.projected_state = {
    stage: "applied", current_step: "已提交",
  };
  actualTransition.preview.target_plan.projected_stage = "applied";
  actualTransition.preview.target_plan.projected_step = "已提交";
  actualTransition.preview.target_plan.projected_applied_date = "2026-07-14";
  assert.equal(isReviewRecordOperation(actualTransition), true);

  const appliedReceipt = appliedOperation();
  appliedReceipt.result.extraction.projected_state = null;
  appliedReceipt.result.derivation.application_before.applied_date = null;
  appliedReceipt.result.derivation.application_after.stage = "applied";
  appliedReceipt.result.derivation.application_after.current_step = "简历筛选";
  appliedReceipt.result.derivation.application_after.current_state_entry_id = 10;
  appliedReceipt.result.derivation.application_after.applied_date = null;
  assert.equal(isReviewRecordOperation(appliedReceipt), true);

  const inferredReceipt = structuredClone(appliedReceipt);
  inferredReceipt.result.derivation.application_after.applied_date = "2026-07-14";
  assert.equal(isReviewRecordOperation(inferredReceipt), false);

  const sameStageReceipt = appliedOperation();
  sameStageReceipt.result.extraction.projected_state = {
    stage: "applied", current_step: "材料复盘",
  };
  sameStageReceipt.result.derivation.application_before.applied_date = null;
  sameStageReceipt.result.derivation.application_after.stage = "applied";
  sameStageReceipt.result.derivation.application_after.current_step = "材料复盘";
  sameStageReceipt.result.derivation.application_after.applied_date = null;
  assert.equal(isReviewRecordOperation(sameStageReceipt), true);

  const inferredSameStageReceipt = structuredClone(sameStageReceipt);
  inferredSameStageReceipt.result.derivation.application_after.applied_date = "2026-07-14";
  assert.equal(isReviewRecordOperation(inferredSameStageReceipt), false);

  const transitionReceipt = appliedOperation();
  transitionReceipt.result.extraction.projected_state = {
    stage: "applied", current_step: "已提交",
  };
  transitionReceipt.result.derivation.application_before.stage = "backlog";
  transitionReceipt.result.derivation.application_before.current_step = null;
  transitionReceipt.result.derivation.application_before.applied_date = null;
  transitionReceipt.result.derivation.application_after.stage = "applied";
  transitionReceipt.result.derivation.application_after.current_step = "已提交";
  transitionReceipt.result.derivation.application_after.applied_date = "2026-07-14";
  assert.equal(isReviewRecordOperation(transitionReceipt), true);
});

test("next-action intent is strict and a clear must remove an existing frozen plan", () => {
  const currentNext = {
    stage: "interviewing", step: "一面", date: "2026-07-20", time: null, note: null,
  };
  const clear = pendingOperation({
    extraction: { clear_next_action: true },
    targetPlan: {
      ...pendingOperation().preview.target_plan,
      kind: "existing",
      application_id: 9,
      created_time: "2026-07-01T10:00:00Z",
      revision: 0,
      current_next_action: currentNext,
      projected_next_action: null,
    },
  });
  assert.equal(isReviewRecordOperation(clear), true);

  const impossibleClear = pendingOperation({ extraction: { clear_next_action: true } });
  assert.equal(isReviewRecordOperation(impossibleClear), false);

  const clearAndSet = pendingOperation({
    extraction: { clear_next_action: true, next_action: currentNext },
  });
  assert.equal(isReviewRecordOperation(clearAndSet), false);

  const missingIntent = pendingOperation();
  delete missingIntent.preview.extraction.clear_next_action;
  assert.equal(isReviewRecordOperation(missingIntent), false);

  const terminalNextTarget = pendingOperation({
    extraction: {
      next_action: {
        stage: "withdrawn", step: "主动撤回", date: null, time: null, note: null,
      },
    },
    targetPlan: {
      ...pendingOperation().preview.target_plan,
      projected_next_action: {
        note: null, time: null, date: null, step: "主动撤回", stage: "withdrawn",
      },
    },
  });
  assert.equal(isReviewRecordOperation(terminalNextTarget), true);

  const closesWithoutExplicitClear = pendingOperation({
    extraction: { projected_state: { stage: "rejected", current_step: null } },
    targetPlan: {
      ...clear.preview.target_plan,
      projected_stage: "rejected",
      projected_step: null,
      projected_next_action: null,
    },
  });
  assert.equal(isReviewRecordOperation(closesWithoutExplicitClear), false);
});

test("rejected confirmation is a terminal zero-result receipt", () => {
  const rejected = appliedOperation({
    state: "rejected",
    outcome: null,
    target_current_state: "voided",
    target_current_revision: 1,
    result: null,
    error: null,
    undo_available: false,
    undo_block_reason: "operation_not_applied",
  });
  assert.equal(isReviewRecordOperation(rejected), true);
  assert.equal(reviewRecordIntegrityIssue(rejected), null);
});

test("state-specific payloads fail closed when preview or terminal fields drift", () => {
  assert.equal(isReviewRecordOperation({ ...pendingOperation(), preview: null }), false);
  assert.equal(isReviewRecordOperation({ ...pendingOperation(), terminal: true }), false);
  assert.equal(isReviewRecordOperation(appliedOperation({
    preview: pendingOperation().preview,
  })), false);
  assert.equal(isReviewRecordOperation(pendingOperation({
    missing: [{ field: "unknown", ask: "补充" }],
  })), false);
  assert.equal(isReviewRecordOperation(pendingOperation({
    missing: [
      { field: "company", ask: "公司？" },
      { field: "company", ask: "还是哪家公司？" },
    ],
  })), false);
  assert.equal(isReviewRecordOperation(pendingOperation({
    targetPlan: {
      kind: "existing",
      application_id: 9,
      company: "Example",
      position: "Engineer",
      created_time: "2026-07-01T10:00:00Z",
      revision: 0,
      current_stage: "applied",
      current_step: null,
      projected_stage: "unknown",
      projected_step: "一面",
      current_next_action: null,
      projected_next_action: null,
      current_applied_date: "2026-07-01",
      projected_applied_date: "2026-07-01",
      current_channel: null,
      projected_channel: null,
    },
  })), false);
  assert.equal(isReviewRecordOperation(pendingOperation({
    targetPlan: {
      ...pendingOperation().preview.target_plan,
      projected_stage: "offer",
    },
  })), false);
  assert.equal(isReviewRecordOperation(pendingOperation({
    targetPlan: {
      ...pendingOperation().preview.target_plan,
      projected_next_action: { stage: "interviewing", step: "二面", date: null, time: null, note: "伪造安排" },
    },
  })), false);
  assert.equal(isReviewRecordOperation(pendingOperation({
    targetPlan: {
      ...pendingOperation().preview.target_plan,
      projected_applied_date: "2026-07-01",
    },
  })), false);
  assert.equal(isReviewRecordOperation(pendingOperation({
    targetPlan: {
      ...pendingOperation().preview.target_plan,
      projected_step: "终面",
    },
  })), false);
  assert.equal(isReviewRecordOperation(appliedOperation({
    state: "rejected",
    outcome: null,
    result: appliedOperation().result,
    undo_available: false,
    undo_block_reason: "operation_not_applied",
  })), false);
});

test("text limits count Unicode code points like the backend", () => {
  const operation = pendingOperation();
  const boundaryCompany = "😀".repeat(200);
  operation.preview.extraction.company = boundaryCompany;
  operation.preview.target_plan.company = boundaryCompany;
  assert.equal(isReviewRecordOperation(operation), true);

  const oversized = structuredClone(operation);
  oversized.preview.extraction.company += "😀";
  oversized.preview.target_plan.company += "😀";
  assert.equal(isReviewRecordOperation(oversized), false);

  const overTotalBudget = pendingOperation();
  overTotalBudget.preview.extraction.questions = Array.from({ length: 13 }, (_, index) => ({
    text: `${index}`.padEnd(4_000, "问"),
    stuck: false,
    knowledge_points: [],
  }));
  assert.equal(isReviewRecordOperation(overTotalBudget), false);
});

test("preview identity normalization and sorted knowledge ids mirror the backend", () => {
  const whitespaceIdentity = pendingOperation();
  whitespaceIdentity.preview.extraction.company = "Ex\u001Cample";
  whitespaceIdentity.preview.target_plan.company = "Example";
  assert.equal(isReviewRecordOperation(whitespaceIdentity), true);

  const unsortedKnowledge = appliedOperation();
  unsortedKnowledge.result.derivation.knowledge_point_ids = [2, 1];
  assert.equal(isReviewRecordOperation(unsortedKnowledge), false);

  const explicitExistingPositionMismatch = appliedOperation();
  explicitExistingPositionMismatch.result.extraction.position = "Designer";
  assert.equal(isReviewRecordOperation(explicitExistingPositionMismatch), false);

  const omittedExistingPosition = appliedOperation();
  omittedExistingPosition.result.extraction.position = null;
  assert.equal(isReviewRecordOperation(omittedExistingPosition), true);
});
