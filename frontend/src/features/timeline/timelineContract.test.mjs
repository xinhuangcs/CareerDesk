import assert from "node:assert/strict";
import { test } from "node:test";

import { ApplicationDetail, Board, TimelineStatistics } from "./timelineContract.ts";

function nextAction(overrides = {}) {
  return {
    stage: "interviewing",
    step: "一面",
    date: "2026-07-22",
    time: "14:30",
    note: null,
    ...overrides,
  };
}

function boardItem(overrides = {}) {
  return {
    id: 7,
    company: "示例公司",
    position: "后端工程师",
    department: null,
    channel: "官网",
    stage: "applied",
    current_step: "简历筛选",
    next_action: nextAction(),
    paused_from_stage: null,
    pause_reason: null,
    priority: null,
    applied_date: "2026-07-19",
    prep_status: "none",
    revision: 3,
    last_activity_time: "2026-07-19T10:00:00+00:00",
    created_time: "2026-07-18T10:00:00+00:00",
    ...overrides,
  };
}

function timelineEntry(overrides = {}) {
  return {
    id: 11,
    step: "完成投递",
    occurred_date: "2026-07-19",
    outcome: "passed",
    summary: null,
    from_stage: "backlog",
    from_step: null,
    to_stage: "applied",
    to_step: "简历筛选",
    source: "manual",
    created_time: "2026-07-19T10:00:00+00:00",
    display_time: "7月19日",
    snapshot_fingerprint: "a".repeat(64),
    ...overrides,
  };
}

function applicationDetail(overrides = {}) {
  return {
    ...boardItem(),
    jd_text: null,
    jd_parsed: { skills: ["TypeScript"], highlights: ["平台研发"] },
    resume_id: 2,
    updated_time: "2026-07-19T10:00:00+00:00",
    prep: { future_artifact: { version: 2 }, error: "可识别但不受详情合同约束" },
    prep_retry_after_seconds: 0,
    timeline_entries: [timelineEntry()],
    application_note: null,
    ...overrides,
  };
}

function clone(value) {
  return structuredClone(value);
}

function expectInvalidDetail(mutator) {
  const payload = clone(applicationDetail());
  mutator(payload);
  assert.throws(
    () => ApplicationDetail.parse(payload),
    { name: "TypeError", message: "岗位详情数据格式异常，请刷新后重试" },
  );
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

test("ApplicationDetail accepts the exact response and treats prep as an open object", () => {
  const payload = applicationDetail();
  assert.strictEqual(ApplicationDetail.parse(payload), payload);
  assert.doesNotThrow(() => ApplicationDetail.parse(applicationDetail({ prep: null })));
  assert.doesNotThrow(() => ApplicationDetail.parse(applicationDetail({ prep: { arbitrary: [1, 2] } })));
});

test("ApplicationDetail rejects invalid top-level stage, revision, extra, and missing keys", () => {
  expectInvalidDetail((payload) => { payload.stage = "onsite"; });
  expectInvalidDetail((payload) => { payload.priority = "urgent"; });
  expectInvalidDetail((payload) => { payload.created_time = ""; });
  expectInvalidDetail((payload) => { payload.revision = -1; });
  expectInvalidDetail((payload) => { payload.unexpected = true; });
  expectInvalidDetail((payload) => { delete payload.application_note; });
});

test("ApplicationDetail rejects nested extras and malformed timeline fingerprints", () => {
  expectInvalidDetail((payload) => { payload.next_action.unexpected = true; });
  expectInvalidDetail((payload) => { payload.jd_parsed.unexpected = true; });
  expectInvalidDetail((payload) => { payload.timeline_entries[0].unexpected = true; });
  expectInvalidDetail((payload) => {
    payload.timeline_entries[0].snapshot_fingerprint = "A".repeat(64);
  });
  expectInvalidDetail((payload) => {
    payload.timeline_entries[0].snapshot_fingerprint = "a".repeat(63);
  });
});

test("ApplicationDetail enforces next-action and terminal-state invariants", () => {
  expectInvalidDetail((payload) => { payload.applied_date = "2026-02-30"; });
  expectInvalidDetail((payload) => { payload.timeline_entries[0].occurred_date = "2026-02-30"; });
  expectInvalidDetail((payload) => { payload.next_action.step = "   "; });
  expectInvalidDetail((payload) => { payload.next_action.date = "2026-02-30"; });
  expectInvalidDetail((payload) => { payload.next_action.time = "24:00"; });
  expectInvalidDetail((payload) => {
    payload.next_action.date = null;
    payload.next_action.time = "09:00";
  });
  expectInvalidDetail((payload) => { payload.stage = "rejected"; });
  expectInvalidDetail((payload) => { payload.pause_reason = "等待 HC"; });
  expectInvalidDetail((payload) => { payload.paused_from_stage = "interviewing"; });
  expectInvalidDetail((payload) => {
    payload.stage = "pooled";
    payload.paused_from_stage = "rejected";
  });
  assert.doesNotThrow(() => ApplicationDetail.parse(applicationDetail({
    stage: "pooled",
    next_action: null,
    paused_from_stage: "interviewing",
    pause_reason: "等待 HC",
  })));
  expectInvalidDetail((payload) => { payload.resume_id = 0; });
  expectInvalidDetail((payload) => { payload.prep_retry_after_seconds = -1; });
  expectInvalidDetail((payload) => { payload.prep = []; });
});

test("Board and upcoming accept exact canonical BoardItem responses", () => {
  const columns = emptyColumns();
  columns.applied.push(boardItem());
  const board = { columns, total: 1 };
  const upcoming = { days: 7, items: [boardItem()] };
  assert.strictEqual(Board.parse(board), board);
  assert.strictEqual(Board.parseUpcoming(upcoming), upcoming);
});

test("Board and upcoming reject drift in their envelopes and nested BoardItems", () => {
  const columns = emptyColumns();
  columns.applied.push(boardItem());

  assert.throws(() => Board.parse({ columns, total: 0 }), TypeError);
  assert.throws(() => Board.parse({ columns, total: 1, unexpected: true }), TypeError);
  const wrongColumn = emptyColumns();
  wrongColumn.applied.push(boardItem({ stage: "interviewing" }));
  assert.throws(() => Board.parse({ columns: wrongColumn, total: 1 }), TypeError);
  const duplicateColumns = clone(columns);
  duplicateColumns.interviewing.push(boardItem({ stage: "interviewing" }));
  assert.throws(() => Board.parse({ columns: duplicateColumns, total: 2 }), TypeError);
  const missingColumn = clone(columns);
  delete missingColumn.pooled;
  assert.throws(() => Board.parse({ columns: missingColumn, total: 1 }), TypeError);

  const badUpcomingStage = { days: 7, items: [boardItem({ stage: "onsite" })] };
  assert.throws(() => Board.parseUpcoming(badUpcomingStage), TypeError);
  const badUpcomingRevision = { days: 7, items: [boardItem({ revision: -1 })] };
  assert.throws(() => Board.parseUpcoming(badUpcomingRevision), TypeError);
  assert.throws(
    () => Board.parseUpcoming({ days: 7, items: [boardItem(), boardItem()] }),
    TypeError,
  );
  const nestedExtra = boardItem();
  nestedExtra.next_action.unexpected = true;
  assert.throws(() => Board.parseUpcoming({ days: 7, items: [nestedExtra] }), TypeError);
  assert.throws(() => Board.parseUpcoming({ items: [boardItem()] }), TypeError);
  assert.throws(
    () => Board.parseUpcoming({ days: 7, items: [boardItem()], unexpected: true }),
    TypeError,
  );
});

test("TimelineStatistics accepts only bounded exact aggregate data", () => {
  const statistics = {
    total_positions: 8,
    submitted: 7,
    active_processes: 3,
    offers: 1,
    rejected: 2,
    withdrawn: 1,
    pooled: 1,
    interview_conversion_percent: 57.1,
    offer_conversion_percent: 14.3,
    funnel: {
      submitted: 7,
      written_test: 3,
      interviewing: 4,
      offer: 1,
      rejected: 2,
    },
  };
  assert.strictEqual(TimelineStatistics.parse(statistics), statistics);
  assert.throws(() => TimelineStatistics.parse({ ...statistics, submitted: 9 }), TypeError);
  assert.throws(
    () => TimelineStatistics.parse({ ...statistics, interview_conversion_percent: 101 }),
    TypeError,
  );
  assert.throws(
    () => TimelineStatistics.parse({
      ...statistics,
      funnel: { ...statistics.funnel, extra: 1 },
    }),
    TypeError,
  );
});
