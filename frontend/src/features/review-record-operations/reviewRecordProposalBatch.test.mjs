import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

import {
  buildReviewRecordProposalBatchDecisions,
  countReviewRecordProposalBatch,
  groupReviewRecordProposalsByTurn,
} from "./reviewRecordProposalBatch.ts";

function operation(clientTurnId, operationId) {
  return {
    client_turn_id: clientTurnId,
    operation_id: operationId,
  };
}

test("Review proposals are grouped by owning turn without changing item order", () => {
  const grouped = groupReviewRecordProposalsByTurn([
    operation("turn-a", "a-1"),
    operation("turn-b", "b-1"),
    operation("turn-a", "a-2"),
  ]);

  assert.deepEqual(grouped.map((batch) => ({
    clientTurnId: batch.clientTurnId,
    operationIds: batch.operations.map((item) => item.operation_id),
  })), [
    { clientTurnId: "turn-a", operationIds: ["a-1", "a-2"] },
    { clientTurnId: "turn-b", operationIds: ["b-1"] },
  ]);
});

test("one batch decision approves selected items and rejects every excluded item", () => {
  const operations = [
    operation("turn-a", "a-1"),
    operation("turn-a", "a-2"),
    operation("turn-a", "a-3"),
  ];

  assert.deepEqual(buildReviewRecordProposalBatchDecisions(
    operations,
    new Set(["a-1", "a-3"]),
  ), [
    { operation_id: "a-1", action: "approve" },
    { operation_id: "a-2", action: "reject" },
    { operation_id: "a-3", action: "approve" },
  ]);
  assert.deepEqual(buildReviewRecordProposalBatchDecisions(
    operations,
    new Set(["a-1", "a-3"]),
    true,
  ), [
    { operation_id: "a-1", action: "reject" },
    { operation_id: "a-2", action: "reject" },
    { operation_id: "a-3", action: "reject" },
  ]);
});

test("an inline child edit travels with the one unified decision", () => {
  const operations = [operation("turn-a", "a-1"), operation("turn-a", "a-2")];
  const edited = {
    company: "修正公司",
    position: "修正岗位",
    channel: "Boss直聘",
    history: { step: "二面", date: "2026-07-18", outcome: "passed", summary: "完成二面" },
    projected_state: { stage: "interviewing", current_step: "二面" },
    clear_next_action: false,
    next_action: null,
    questions: [],
    mood: null,
    time_of_day: null,
    factors: [],
  };

  assert.deepEqual(buildReviewRecordProposalBatchDecisions(
    operations,
    new Set(["a-1"]),
    false,
    new Map([["a-1", edited], ["a-2", { ...edited, company: "不会发送" }]]),
  ), [
    { operation_id: "a-1", action: "approve", edited_extraction: edited },
    { operation_id: "a-2", action: "reject" },
  ]);
});

test("batch write and draft counts follow edited job identity", () => {
  const draft = {
    ...operation("turn-a", "draft"),
    preview: {
      extraction: { company: "清华大学", position: null },
      missing: [{ field: "position", ask: "请补充岗位" }],
    },
  };
  const complete = {
    ...operation("turn-a", "complete"),
    preview: {
      extraction: { company: "北京大学", position: "助理教授" },
      missing: [],
    },
  };
  const operations = [draft, complete];
  const included = new Set(["draft", "complete"]);

  assert.deepEqual(countReviewRecordProposalBatch(operations, included), {
    includedCount: 2,
    publishCount: 1,
    retainedDraftCount: 1,
    excludedCount: 0,
  });
  assert.deepEqual(countReviewRecordProposalBatch(
    operations,
    included,
    new Map([["draft", { company: "清华大学", position: "教授" }]]),
  ), {
    includedCount: 2,
    publishCount: 2,
    retainedDraftCount: 0,
    excludedCount: 0,
  });
  assert.deepEqual(countReviewRecordProposalBatch(
    operations,
    included,
    new Map([
      ["draft", { company: "清华大学", position: "教授" }],
      ["complete", { company: "北京大学", position: null }],
    ]),
  ), {
    includedCount: 2,
    publishCount: 1,
    retainedDraftCount: 1,
    excludedCount: 0,
  });
});

test("a batch decision rejects mixed turns, duplicates, and unknown selections", () => {
  assert.throws(
    () => buildReviewRecordProposalBatchDecisions([], new Set()),
    /1–50 unique records/,
  );
  assert.throws(
    () => buildReviewRecordProposalBatchDecisions([
      operation("turn-a", "a-1"),
      operation("turn-b", "b-1"),
    ], new Set()),
    /1–50 unique records/,
  );
  assert.throws(
    () => buildReviewRecordProposalBatchDecisions([
      operation("turn-a", "a-1"),
      operation("turn-a", "a-1"),
    ], new Set()),
    /1–50 unique records/,
  );
  assert.throws(
    () => buildReviewRecordProposalBatchDecisions([
      operation("turn-a", "a-1"),
    ], new Set(["other"])),
    /unknown proposal/,
  );
});

test("the unified decision helper supports the full fifty-item Review batch", () => {
  const operations = Array.from({ length: 50 }, (_, index) => (
    operation("turn-a", `operation-${index + 1}`)
  ));
  const decisions = buildReviewRecordProposalBatchDecisions(
    operations,
    new Set(operations.map((item) => item.operation_id)),
  );

  assert.equal(decisions.length, 50);
  assert.ok(decisions.every((decision) => decision.action === "approve"));
  assert.throws(
    () => buildReviewRecordProposalBatchDecisions(
      [...operations, operation("turn-a", "operation-51")],
      new Set(),
    ),
    /1–50 unique records/,
  );
});

test("confirmation UI edits fact, projected state, and next action without legacy classifications", async () => {
  const editor = await readFile(new URL("./ReviewRecordProposalBatchCard.tsx", import.meta.url), "utf8");
  const card = await readFile(new URL("./ReviewRecordOperationCard.tsx", import.meta.url), "utf8");
  for (const source of [editor, card]) {
    assert.match(source, /本次发生/);
    assert.match(source, /确认后的阶段与环节/);
    assert.match(source, /下一步安排/);
    assert.doesNotMatch(source, /event_type|latest_round|step_state|waiting_on|next_event_date/);
    assert.doesNotMatch(source, /面试轮次|历程类型/);
  }
  assert.match(editor, /复盘后的当前环节/);
  assert.match(editor, /下一步时间/);
  assert.match(editor, /完成后阶段（可不变）/);
  assert.match(editor, /和岗位当前值相同/);
  assert.match(editor, /流程结束后不能保留下一步/);
  assert.match(editor, /clear_next_action: hasCurrentNextAction/);
  assert.match(editor, /current_next_action != null/);
  assert.match(editor, /<option value="clear" disabled=\{!hasCurrentNextAction\}>/);
  assert.match(editor, /<option value="preserve" disabled=\{terminalPlanMustClear\}>/);
  assert.match(editor, /<option value="set" disabled=\{terminalProjection\}>/);
  assert.match(editor, /流程结束后不能保留未来安排/);
  assert.match(editor, /const projectedStage = extraction\.projected_state\?\.stage \?\? currentStage/);
  assert.match(editor, /留空保持/);
  assert.match(editor, /stageLabel\(finalStage, english\)/);
  assert.match(editor, /Stage and step after confirmation/);
  assert.match(editor, /targetPlan\?\.projected_stage/);
  assert.match(editor, /targetPlan\?\.projected_step/);
  assert.match(editor, /targetPlan\?\.projected_applied_date/);
  assert.match(editor, /targetPlan\?\.projected_channel/);
  assert.match(editor, /targetPlan\?\.projected_next_action/);
  assert.doesNotMatch(editor, /JSON\.stringify/);
  assert.match(editor, /identityLocked=\{targetPlan != null\}/);
  assert.match(editor, /disabled=\{identityLocked\}/);
  assert.match(editor, /本次确认已锁定岗位/);
  assert.match(editor, /清空现有安排/);
  assert.match(editor, /保持现有安排/);
  assert.match(card, /完成后进入/);
  assert.doesNotMatch(editor, />下一阶段</);
  assert.doesNotMatch(editor, /当前状态变化/);
});
