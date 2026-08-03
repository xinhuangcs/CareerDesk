import assert from "node:assert/strict";
import { test } from "node:test";

import {
  captureBoardItem,
  captureListItem,
  codePointLength,
  confirmHistoryDraftAgainstLatest,
  formatNextActionForImpact,
  limitCodePoints,
  limitCodePointsWhileEditing,
  projectStageMove,
  rebaseExistingHistoryDraftAfterConflict,
  rebaseHistoryDraftAfterConflict,
  removeApplicationFromBoard,
  restoreBoardItem,
  restoreListItem,
} from "./timelineInteractionState.ts";

function nextAction(overrides = {}) {
  return {
    stage: "interviewing",
    step: "二面",
    date: "2026-07-22",
    time: "14:30",
    note: null,
    ...overrides,
  };
}

function boardItem(id, stage = "applied", overrides = {}) {
  return {
    id,
    company: `公司 ${id}`,
    position: "工程师",
    department: null,
    channel: null,
    stage,
    current_step: null,
    next_action: nextAction(),
    revision: 3,
    priority: null,
    applied_date: null,
    prep_status: "none",
    last_activity_time: null,
    created_time: `2026-07-${String(id).padStart(2, "0")}T00:00:00+00:00`,
    pause_reason: null,
    paused_from_stage: null,
    ...overrides,
  };
}

function emptyColumns() {
  return {
    backlog: [],
    applied: [],
    written_test: [],
    interviewing: [],
    offer: [],
    withdrawn: [],
    rejected: [],
    pooled: [],
  };
}

test("terminal optimistic projection clears the plan and active projection preserves it", () => {
  const source = boardItem(1);
  const rejected = projectStageMove(source, "rejected");
  assert.equal(rejected.stage, "rejected");
  assert.equal(rejected.next_action, null);
  assert.notStrictEqual(rejected, source);
  assert.deepEqual(projectStageMove(source, "interviewing").next_action, source.next_action);
});

test("failed stage move restores the exact card without reverting unrelated concurrent cards", () => {
  const columns = emptyColumns();
  const original = boardItem(1, "applied");
  columns.applied = [boardItem(9, "applied"), original, boardItem(8, "applied")];
  const snapshot = captureBoardItem(columns, 1);
  assert.ok(snapshot);

  const duringFailure = emptyColumns();
  duringFailure.applied = [boardItem(9, "applied", { priority: "high" })];
  duringFailure.interviewing = [boardItem(8, "interviewing", { revision: 4 })];
  duringFailure.rejected = [projectStageMove(original, "rejected")];

  const restored = restoreBoardItem(duringFailure, snapshot);
  assert.deepEqual(restored.applied.map((item) => item.id), [9, 1]);
  assert.equal(restored.applied[0].priority, "high");
  assert.equal(restored.interviewing[0].revision, 4);
  assert.deepEqual(restored.applied[1], original);
  assert.equal(restored.rejected.length, 0);
});

test("failed stage move restores or removes only the matching upcoming item", () => {
  const original = boardItem(1);
  const other = boardItem(2, "interviewing", { revision: 7 });
  const snapshot = captureListItem([other, original], 1);
  assert.ok(snapshot);
  assert.deepEqual(
    restoreListItem([other, projectStageMove(original, "rejected")], 1, snapshot),
    [other, original],
  );
  assert.deepEqual(restoreListItem([other], 1, snapshot), [other, original]);
  assert.deepEqual(restoreListItem([other], 1, null), [other]);
});

test("a missing application is removed from its board snapshot without touching other jobs", () => {
  const columns = emptyColumns();
  columns.applied = [boardItem(1), boardItem(2)];
  const board = { columns, total: 2 };
  const removed = removeApplicationFromBoard(board, 1);
  assert.equal(removed.total, 1);
  assert.deepEqual(removed.columns.applied.map((item) => item.id), [2]);
  assert.strictEqual(removeApplicationFromBoard(removed, 99), removed);
});

test("history conflict safely rebases facts but requires confirmation for projections", () => {
  const factual = rebaseHistoryDraftAfterConflict({
    expected_revision: 2,
    update_current_state: false,
    set_next_action: false,
    conflicted: false,
    summary: "补记沟通",
  }, 5);
  assert.equal(factual.kind, "safe_rebase");
  assert.equal(factual.draft.expected_revision, 5);
  assert.equal(factual.draft.conflicted, false);
  assert.equal(factual.draft.summary, "补记沟通");

  for (const changing of [
    { update_current_state: true, set_next_action: false },
    { update_current_state: false, set_next_action: true },
  ]) {
    const resolved = rebaseHistoryDraftAfterConflict({
      expected_revision: 2,
      conflicted: false,
      ...changing,
    }, 5);
    assert.equal(resolved.kind, "confirmation_required");
    assert.equal(resolved.draft.expected_revision, 5);
    assert.equal(resolved.draft.conflicted, true);
    const confirmed = confirmHistoryDraftAgainstLatest(resolved.draft);
    assert.equal(confirmed.expected_revision, 5);
    assert.equal(confirmed.conflicted, false);
  }
});

test("existing history edits three-way merge independent server changes", () => {
  const base = {
    step: "一面",
    occurred_date: "2026-07-18",
    outcome: "",
    summary: "和招聘经理沟通",
  };
  const result = rebaseExistingHistoryDraftAfterConflict(
    base,
    {
      ...base,
      summary: "和招聘经理深入沟通",
      expected_revision: 3,
      expected_fingerprint: "old",
      conflicted: false,
    },
    { ...base, outcome: "passed" },
    7,
    "new",
  );

  assert.equal(result.kind, "safe_rebase");
  assert.equal(result.conflict, null);
  assert.equal(result.draft.summary, "和招聘经理深入沟通");
  assert.equal(result.draft.outcome, "passed");
  assert.equal(result.draft.expected_revision, 7);
  assert.equal(result.draft.expected_fingerprint, "new");
  assert.equal(result.draft.conflicted, false);
});

test("existing history edits retain the draft and expose semantic conflicts", () => {
  const base = {
    step: "一面",
    occurred_date: "2026-07-18",
    outcome: "",
    summary: "原说明",
  };
  const result = rebaseExistingHistoryDraftAfterConflict(
    base,
    {
      ...base,
      summary: "我的说明",
      expected_revision: 3,
      expected_fingerprint: "old",
      conflicted: false,
    },
    { ...base, summary: "另一窗口的说明" },
    8,
    "latest",
  );

  assert.equal(result.kind, "confirmation_required");
  assert.deepEqual(result.conflict?.fields, ["summary"]);
  assert.equal(result.conflict?.latest.summary, "另一窗口的说明");
  assert.equal(result.draft.summary, "我的说明");
  assert.equal(result.draft.expected_revision, 8);
  assert.equal(result.draft.expected_fingerprint, "latest");
  assert.equal(result.draft.conflicted, true);
});

test("terminal-stage warning names the exact plan and schedule", () => {
  assert.equal(formatNextActionForImpact(nextAction()), "“二面”（2026-07-22 14:30）");
  assert.equal(
    formatNextActionForImpact(nextAction({ date: null, time: null })),
    "“二面”",
  );
});

test("text limits count Unicode code points instead of UTF-16 units", () => {
  assert.equal(codePointLength("岗位😀😀"), 4);
  assert.equal(limitCodePoints("😀😀😀", 2), "😀😀");
  assert.equal(limitCodePoints("工程师", 20), "工程师");
  assert.equal(limitCodePointsWhileEditing("😀😀😀😀", "😀😀😀", 2), "😀😀😀");
  assert.equal(limitCodePointsWhileEditing("😀😀😀", "😀😀😀a", 2), "😀😀");
});
