import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { mergeConflictingApplicationNote } from "./timelineNoteMerge.ts";
import {
  mergeConflictingApplicationProfile,
  timelineProfileDraftChanged,
} from "./timelineProfileMerge.ts";
import {
  boardCardMeta,
  COLUMNS,
  historyEntryIsMeaningful,
  matchesTimelineQuery,
  nextActionSummary,
  nextStageAfterCurrentStageChange,
  sortListRows,
  timelineEntryDateLabel,
  timelineEntryNodeClass,
  timelineEntrySummary,
  timelineEntryTitle,
  upcomingDayGroups,
} from "./timelineDisplay.ts";

const pageUrl = new URL("./TimelinePage.tsx", import.meta.url);
const createDialogUrl = new URL("./TimelineCreateApplicationDialog.tsx", import.meta.url);

function nextAction(overrides = {}) {
  return {
    stage: "interviewing",
    step: "二面",
    date: null,
    time: null,
    note: null,
    ...overrides,
  };
}

function boardItem(overrides = {}) {
  return {
    id: 1,
    company: "字节跳动",
    position: "后端开发工程师",
    department: null,
    stage: "applied",
    current_step: null,
    next_action: null,
    revision: 0,
    priority: null,
    applied_date: null,
    prep_status: "none",
    last_activity_time: "2026-07-10T00:00:00+00:00",
    created_time: "2026-07-09T00:00:00+00:00",
    channel: null,
    pause_reason: null,
    paused_from_stage: null,
    ...overrides,
  };
}

function entry(overrides = {}) {
  return {
    id: 1,
    step: null,
    occurred_date: null,
    outcome: null,
    summary: null,
    from_stage: "applied",
    from_step: null,
    to_stage: "applied",
    to_step: null,
    source: "manual",
    created_time: "2026-07-15T02:40:00+00:00",
    ...overrides,
  };
}

test("board stages preserve familiar job-search language", () => {
  assert.deepEqual(COLUMNS, [
    ["backlog", "待定"],
    ["applied", "已投递"],
    ["written_test", "笔试中"],
    ["interviewing", "面试中"],
    ["offer", "Offer"],
    ["pooled", "泡池子"],
    ["withdrawn", "不再跟进"],
    ["rejected", "已挂"],
  ]);
});

test("board cards and agenda read only the canonical next_action", () => {
  const scheduled = boardCardMeta(
    boardItem({ stage: "interviewing", next_action: nextAction({ step: "三面", date: "2026-07-20" }) }),
    "2026-07-19",
  );
  assert.deepEqual(scheduled, { when: "7-20", tone: "near", what: "三面" });

  const groups = upcomingDayGroups([
    boardItem({ id: 1, stage: "interviewing", priority: "low", next_action: nextAction({ date: "2026-07-20" }) }),
    boardItem({ id: 2, stage: "applied", priority: "high", next_action: nextAction({ stage: "applied", step: "联系 HR", date: "2026-07-20" }) }),
    boardItem({ id: 3, stage: "pooled", next_action: nextAction({ date: "2026-07-21" }) }),
    boardItem({ id: 4, stage: "rejected", next_action: null }),
  ], "2026-07-19");
  assert.equal(groups.length, 1);
  assert.equal(groups[0].items.length, 2);
  assert.deepEqual(groups[0].items.map((item) => item.id), [2, 1]);
  assert.equal(groups[0].dateLabel, "7月20日");
});

test("paused and terminal stages never expose a pending countdown", () => {
  const paused = boardCardMeta(boardItem({
    stage: "pooled",
    next_action: nextAction({ date: "2026-07-25" }),
    paused_from_stage: "interviewing",
  }), "2026-07-19");
  assert.equal(paused.when, null);
  assert.match(paused.what, /流程暂停/);

  const rejected = boardCardMeta(boardItem({ stage: "rejected" }), "2026-07-19");
  assert.equal(rejected.when, null);
  assert.equal(rejected.what, "公司已结束流程");
});

test("timeline titles prefer step, then transition, then summary", () => {
  assert.equal(timelineEntryTitle(entry({ step: "二面", to_step: "二面" })), "二面");
  assert.equal(timelineEntryTitle(entry({ from_stage: "applied", to_stage: "interviewing" })), "进入面试中");
  assert.equal(timelineEntryTitle(entry({ summary: "联系 HR\n确认时间" })), "联系 HR");
  assert.equal(timelineEntryTitle(entry()), "进展更新");
});

test("known system timeline summaries follow the UI locale without translating user content", () => {
  assert.equal(timelineEntrySummary(entry({
    source: "agent",
    from_stage: "backlog",
    to_stage: "written_test",
    summary: "批量导入新增岗位并设为「笔试中」",
  }), "en"), "Added via bulk import and set to Assessment");
  assert.equal(timelineEntrySummary(entry({
    source: "agent",
    from_stage: "backlog",
    to_stage: "applied",
    summary: "投递",
  }), "en"), "Application submitted");
  assert.equal(timelineEntrySummary(entry({
    source: "manual",
    from_stage: "backlog",
    to_stage: "interviewing",
    summary: "新增岗位并设为「面试中」",
  }), "en"), "Application added and set to Interviewing");
  assert.equal(timelineEntrySummary(entry({
    source: "drag",
    from_stage: "written_test",
    to_stage: "interviewing",
    summary: "从「笔试中」拖到「面试中」",
  }), "en"), "Moved from Assessment to Interviewing");
  assert.equal(timelineEntrySummary(entry({
    source: "manual",
    from_stage: "applied",
    to_stage: "pooled",
    summary: "阶段从「已投递」调整为「泡池子」",
  }), "en"), "Stage changed from Applied to On hold");
  assert.equal(timelineEntrySummary(entry({
    source: "manual",
    step: "Online coding test",
    summary: "完成「Online coding test」",
  }), "en"), "Completed “Online coding test”");
  assert.equal(timelineEntrySummary(entry({
    source: "manual",
    summary: "候选人希望下周再联系",
  }), "en"), "候选人希望下周再联系");
  assert.equal(timelineEntrySummary(entry({
    source: "review",
    from_stage: "backlog",
    to_stage: "written_test",
    summary: "批量导入新增岗位并设为「笔试中」",
  }), "en"), "批量导入新增岗位并设为「笔试中」");
});

test("timeline colours come from outcome and destination stage", () => {
  assert.equal(timelineEntryNodeClass(entry({ outcome: "passed" })), "bg-ok");
  assert.equal(timelineEntryNodeClass(entry({ outcome: "failed" })), "bg-bad");
  assert.equal(timelineEntryNodeClass(entry({ to_stage: "interviewing" })), "bg-info");
  assert.equal(timelineEntryNodeClass(entry({ to_stage: "written_test" })), "bg-warn");
});

test("next action summary and timeline dates have stable fallbacks", () => {
  assert.match(nextActionSummary({
    stage: "interviewing",
    next_action: nextAction({ step: "三面", date: "2026-07-20", time: "14:00" }),
  }, "2026-07-19"), /^下一步 · 三面 · 7-20 .* · 14:00$/);
  assert.equal(nextActionSummary({ stage: "pooled", next_action: nextAction() }, "2026-07-19"), null);
  assert.match(timelineEntryDateLabel(entry({ occurred_date: "2026-07-14" }), "2026-07-19"), /^7-14 /);
  assert.match(timelineEntryDateLabel(entry(), "2026-07-19"), /^7-15 /);
});

test("history facts can exist without changing the current projection", () => {
  assert.equal(historyEntryIsMeaningful(entry()), false);
  assert.equal(historyEntryIsMeaningful(entry({ step: "一面" })), true);
  assert.equal(historyEntryIsMeaningful(entry({ occurred_date: "2026-07-19" })), false);
  assert.equal(historyEntryIsMeaningful(entry({ outcome: "passed" })), true);
  assert.equal(historyEntryIsMeaningful(entry({ summary: "已联系 HR" })), true);
  assert.equal(historyEntryIsMeaningful(entry({ to_stage: "interviewing" })), true);
});

test("create form follows current stage until the target stage is explicitly edited", () => {
  assert.equal(nextStageAfterCurrentStageChange("backlog", "applied", false), "applied");
  assert.equal(nextStageAfterCurrentStageChange("written_test", "applied", true), "written_test");
  assert.equal(nextStageAfterCurrentStageChange("interviewing", "rejected", true), "interviewing");
  assert.equal(nextStageAfterCurrentStageChange("applied", "withdrawn", false), "applied");
});

test("list sorting uses priority, then newest added time", () => {
  const sorted = sortListRows([
    boardItem({ id: 1, priority: null, created_time: "2026-07-20T00:00:00+00:00" }),
    boardItem({ id: 2, priority: "medium", created_time: "2026-07-22T00:00:00+00:00" }),
    boardItem({ id: 3, priority: "high", created_time: "2026-07-18T00:00:00+00:00" }),
    boardItem({ id: 4, priority: "low", created_time: "2026-07-23T00:00:00+00:00" }),
    boardItem({ id: 5, priority: "high", created_time: "2026-07-21T00:00:00+00:00" }),
  ]);
  assert.deepEqual(sorted.map((item) => item.id), [5, 3, 2, 4, 1]);
});

test("search remains case and width insensitive", () => {
  assert.equal(matchesTimelineQuery(boardItem({ company: "Google DeepMind" }), "google"), true);
  assert.equal(matchesTimelineQuery(boardItem({ channel: "LinkedIn" }), "LINKEDIN"), true);
  assert.equal(matchesTimelineQuery(boardItem({ company: "ＡＣＭＥ" }), "acme"), true);
});

test("profile conflict merge uses stage and revision", () => {
  const base = {
    expected_revision: 1,
    company: "Google",
    position: "Engineer",
    department: "Cloud",
    channel: "官网",
    stage: "applied",
    current_step: "简历筛选",
    applied_date: "2026-07-01",
    pause_reason: "",
    jd_text: "v1",
  };
  const local = { ...base, company: "Google DeepMind", jd_text: "local JD" };
  const saved = { ...base, expected_revision: 2, department: "Research", jd_text: "server JD", stage: "interviewing" };
  const merged = mergeConflictingApplicationProfile(base, local, saved);
  assert.equal(merged.draft.stage, "interviewing");
  assert.equal(merged.draft.jd_text, "local JD");
  assert.deepEqual(merged.conflictingFields, ["jd_text"]);
  assert.equal(timelineProfileDraftChanged(merged.draft, saved), true);
});

test("profile conflict merge includes applied date and pooled reason", () => {
  const base = {
    expected_revision: 4,
    company: "Acme",
    position: "Engineer",
    department: "",
    channel: "",
    stage: "pooled",
    current_step: "等待结果",
    applied_date: "2026-07-01",
    pause_reason: "等待 HC",
    jd_text: "",
  };
  const local = { ...base, pause_reason: "等待预算" };
  const saved = {
    ...base,
    expected_revision: 5,
    applied_date: "2026-07-02",
    pause_reason: "暂缓招聘",
  };
  const merged = mergeConflictingApplicationProfile(base, local, saved);
  assert.equal(merged.draft.applied_date, "2026-07-02");
  assert.equal(merged.draft.pause_reason, "等待预算");
  assert.deepEqual(merged.conflictingFields, ["pause_reason"]);
});

test("note conflict merge preserves both users' changes", () => {
  assert.equal(
    mergeConflictingApplicationNote("原备注", "原备注（用户已改）", "原备注\nAgent 新增"),
    "原备注（用户已改）\nAgent 新增",
  );
});

test("page exposes the clean projection, next-action, history, and CAS interactions", async () => {
  const source = await readFile(pageUrl, "utf8");
  assert.doesNotMatch(source, /id="current-application-state"/);
  assert.match(source, /id="next-action-title"/);
  assert.match(source, /当前环节/);
  assert.match(source, /完成下一步/);
  assert.match(source, /继续安排新的下一步/);
  assert.match(source, /同时设置新的下一步/);
  assert.match(source, /同时更新当前阶段 \/ 环节/);
  assert.match(source, /l\("完成后将进入"[\s\S]*?stageLabel\(completionTargetStage/);
  assert.match(source, /更新后流程结束，现有下一步会自动清空/);
  assert.match(source, /const completionClosesApplication = completionTargetStage !== null/);
  assert.match(source, /TERMINAL_STAGES\.includes\(projectedStage\)/);
  assert.match(source, /entry\.source === "review"/);
  assert.match(source, /撤销整次复盘/);
  assert.match(source, /prepareTimelineReviewUndoOperation/);
  assert.match(source, /核对完整影响后撤销/);
  assert.match(source, /\/complete-next-action/);
  assert.match(source, /\/progress/);
  assert.match(source, /expected_revision/);
  assert.doesNotMatch(source, /event_type|latest_round|step_state|waiting_on|next_event_date|edit_revision/);
  assert.doesNotMatch(source, /面试轮次|历程进展/);
});

test("desktop board fills the space left by an optional seven-day agenda", async () => {
  const source = await readFile(pageUrl, "utf8");
  assert.match(source, /\{dayGroups\.length > 0 && \(/);
  assert.match(source, /className="flex flex-col gap-5 md:h-full md:min-h-0"/);
  assert.match(source, /className="md:min-h-0 md:flex-1">\s*<TimelineBoard/);
  assert.match(source, /md:max-h-none md:min-h-0 md:flex-1/);
  assert.doesNotMatch(source, /max-h-\[64vh\][^"\n]*md:max-h-\[64vh\]/);
});

test("seven-day agenda collapses from its footer into one compact row", async () => {
  const source = await readFile(pageUrl, "utf8");
  assert.match(source, /upcomingCollapsed, setUpcomingCollapsed/);
  assert.match(source, /aria-controls="timeline-upcoming-content"/);
  assert.match(source, /onClick=\{\(\) => setUpcomingCollapsed\(true\)\}/);
  assert.match(source, /onClick=\{\(\) => setUpcomingCollapsed\(false\)\}/);
  assert.match(source, /aria-expanded="false"[\s\S]*?<span className="section-label">\{l\("接下来 7 天"/);
  assert.match(source, /l\("收起", "Collapse"\)[\s\S]*?<span aria-hidden="true">↑<\/span>/);
});

test("application detail mounts the controlled resume adaptation feature", async () => {
  const source = await readFile(pageUrl, "utf8");
  assert.match(source, /ResumeAdaptationPanel/);
  assert.match(source, /\["adaptation", l\("简历优化", "Resume adaptation"\)\]/);
  assert.match(source, /applicationId=\{detail\.id\}/);
  assert.match(source, /editRevision=\{detail\.revision\}/);
  assert.match(source, /onApplicationChanged/);
  assert.match(source, /onResearchAction/);
  assert.match(source, /refreshResearch: action === "refresh" \|\| action === "restart"/);
  assert.match(source, /tab === "adaptation"/);
  assert.match(source, /tab === "research"/);
  assert.match(source, /void loadBriefing\(applicationId\)/);
  assert.match(source, /detailTab === "adaptation"/);
  assert.doesNotMatch(source, /简历适配候选|简历匹配（当前默认）|adaptationCandidateOpen|showLegacyMatch/);
  assert.doesNotMatch(source, /className="order-[12] rounded-xl border border-line bg-panel-2/);
  assert.doesNotMatch(source, /\/resume-match/);
});

test("dragging is an explicit stage-only optimistic CAS command", async () => {
  const source = await readFile(pageUrl, "utf8");
  const move = source.match(/async function moveApplicationStage[\s\S]*?\n    \} catch \(e\) \{/)?.[0] ?? "";
  assert.ok(move);
  assert.match(move, /\/stage/);
  assert.match(move, /expected_revision: item\.revision/);
  assert.match(move, /stage: targetStage/);
  assert.match(move, /origin: "board_drag" \| "detail_menu"/);
  assert.match(move, /`\/api\/timeline\/applications\/\$\{item\.id\}\/stage`, \{\s*expected_revision: item\.revision,\s*stage: targetStage,\s*origin,/);
  assert.match(source, /moveApplicationStage\(detail, target as ApplicationStage, "detail_menu"\)/);
  assert.match(source, /moveApplicationStage\(item, stage, "board_drag"\)/);
  assert.doesNotMatch(move, /expected_stage/);
  assert.match(move, /settledStageOverridesRef\.current\.set/);
  assert.match(
    move,
    /setStageMoveNotices\([\s\S]*?void refresh\(selected, selectionEpochRef\.current\)/,
    "a successful move must immediately refresh upcoming so a resumed pooled plan is reinserted",
  );
  assert.match(source, /useState<Record<number, StageMoveNotice>>\(\{\}\)/);
  assert.match(source, /useRef<Map<number, number>>\(new Map\(\)\)/);
  assert.match(move, /\[item\.id\]: \{\s*kind: "saved"/);
  assert.match(source, /\[item\.id\]: \{\s*kind: "error"/);
  assert.match(move, /delete next\[item\.id\]/);
  const failedMove = source.match(/\} catch \(e\) \{[\s\S]*?await refresh\(selected, selectionEpochRef\.current\);/)?.[0] ?? "";
  assert.doesNotMatch(failedMove, /setError\(`/);
});

test("moving a planned job into a closed stage requires explicit confirmation", async () => {
  const source = await readFile(pageUrl, "utf8");
  assert.match(source, /stageEndsApplication\(targetStage\)/);
  assert.match(source, /item\.next_action !== null/);
  assert.match(source, /window\.confirm\(/);
  assert.match(source, /会清空下一步/);
});

test("next-action drafts rebase only when the frozen plan itself is unchanged", async () => {
  const source = await readFile(pageUrl, "utf8");
  assert.match(source, /base_next_action: NextAction \| null/);
  assert.match(source, /expected_next_action: NextAction/);
  assert.match(source, /sameNextAction\(/);
  assert.match(source, /expected_revision: authoritative\.revision/);
  assert.match(source, /草稿已安全换到最新版本/);
  assert.match(source, /conflicted: true/);
  assert.match(source, /不会覆盖最新安排/);
  assert.match(source, /编辑最新安排/);
  const missing = source.match(/const clearMissingApplication[\s\S]*?setBoard\(/)?.[0] ?? "";
  assert.match(missing, /setNextActionDraft\(null\)/);
  assert.match(missing, /setCompletionDraft\(null\)/);
  assert.match(missing, /setNextActionSaving\(false\)/);
});

test("unified detail edit controls and atomically saves the next-action draft", async () => {
  const source = await readFile(pageUrl, "utf8");
  assert.match(
    source,
    /aria-label=\{l\("下一步日期", "Next-action date"\)\} value=\{nextActionDraft\.date\} onChange=\{\(event\) => setNextActionDraft\(\{ \.\.\.nextActionDraft, date: event\.target\.value,/,
  );
  assert.match(
    source,
    /aria-label=\{l\("下一步时间", "Next-action time"\)\} value=\{nextActionDraft\.time\} disabled=\{!nextActionDraft\.date\}/,
  );
  const start = source.match(/function startDetailEdit\(\)[\s\S]*?\n  }/)?.[0] ?? "";
  assert.match(start, /setNextActionDraft\(nextActionDraftFromDetail\(detail\)\)/);
  const save = source.match(/async function saveDetailEdit\(\)[\s\S]*?\n  }\n\n  async function prepareDetailDelete/)?.[0] ?? "";
  assert.ok(save);
  assert.match(save, /const desiredNextAction = stageEndsApplication/);
  assert.match(save, /profileDirty \|\| nextActionDirty/);
  assert.match(save, /`\/api\/timeline\/applications\/\$\{id\}\/profile`/);
  assert.match(save, /next_action: desiredNextAction/);
  assert.match(source, /nextActionDraftInvalid = nextActionDraft !== null\s*&& !stageEndsApplication\(profileDraft\?\.stage \?\? detail\?\.stage \?\? nextActionDraft\.stage\)/);
  assert.doesNotMatch(source, /async function saveNextAction/);
  assert.doesNotMatch(source, /function editNextAction/);
});

test("history child-resource misses recheck the parent and retain recoverable edits", async () => {
  const source = await readFile(pageUrl, "utf8");
  const reconcile = source.match(
    /async function reconcileMissingTimelineEntry[\s\S]*?\n  }\n\n  const refresh/,
  )?.[0] ?? "";
  assert.ok(reconcile);
  assert.match(reconcile, /await loadDetail\(/);
  assert.match(reconcile, /setExternalChangeNotice\(existingApplicationMessage\)/);
  assert.doesNotMatch(reconcile, /clearMissingApplication\(/);

  const prepareUndo = source.match(/async function prepareReviewEntryUndo[\s\S]*?\n  }\n/)?.[0] ?? "";
  const deleteEntry = source.match(/async function performHistoryEntryDelete[\s\S]*?\n  }\n/)?.[0] ?? "";
  assert.match(prepareUndo, /status === 404[\s\S]*?reconcileMissingTimelineEntry\(/);
  assert.match(deleteEntry, /status === 404[\s\S]*?reconcileMissingTimelineEntry\(/);
  assert.match(source, /status === 404 && historyWritePending[\s\S]*?reconcileMissingTimelineEntry\(/);
});

test("concurrent edits to one history entry use a three-way retry decision", async () => {
  const source = await readFile(pageUrl, "utf8");
  assert.match(source, /rebaseHistoryEditDraftsAgainstLatest\(/);
  assert.match(source, /仍用我的草稿覆盖/);
  assert.match(source, /放弃这条草稿，使用最新值/);
  assert.match(source, /dirtyHistoryDrafts\.some\(\(draft\) => draft\.conflicted\)/);
});

test("profile business conflicts keep the draft and surface the server reason", async () => {
  const source = await readFile(pageUrl, "utf8");
  const businessConflict = source.match(
    /latestProfile\.expected_revision === baseAtConflict\.expected_revision[\s\S]*?return;/,
  )?.[0] ?? "";
  assert.ok(businessConflict);
  assert.match(businessConflict, /setProfileConflict\(null\)/);
  assert.match(businessConflict, /setError\(e\.message/);
});

test("priority updates share the application CAS and consume the authoritative detail", async () => {
  const source = await readFile(pageUrl, "utf8");
  const toggle = source.match(/async function changePriority[\s\S]*?\n  }\n/)?.[0] ?? "";
  assert.ok(toggle);
  assert.match(toggle, /ApplicationDetail\.parse\(await putJson<unknown>/);
  assert.match(toggle, /expected_revision: currentDetail\.revision/);
  assert.match(toggle, /priority: loaded\.priority/);
  assert.match(toggle, /\/priority/);
  assert.match(toggle, /revision: loaded\.revision/);
  assert.match(toggle, /e instanceof HttpError && e\.status === 409/);
  assert.match(toggle, /await refresh\(stillSelected \? id : undefined, selectionEpoch\)/);
});

test("all ApplicationDetail responses cross the runtime contract boundary", async () => {
  const source = await readFile(pageUrl, "utf8");
  const dialog = await readFile(createDialogUrl, "utf8");
  assert.doesNotMatch(source, /(?:getJson|putJson|postJson|del)<ApplicationDetail>/);
  assert.doesNotMatch(dialog, /postJson<ApplicationDetail>/);
  assert.match(source, /Board\.parse\(boardPayload\)/);
  assert.match(source, /Board\.parseUpcoming\(upcomingPayload\)/);
  assert.match(dialog, /ApplicationDetail\.parse\(await postJson<unknown>/);
});

test("all stages collapse independently with the last three closed by default", async () => {
  const source = await readFile(pageUrl, "utf8");
  const board = source.match(/function TimelineBoard[\s\S]*?\n}\n/)?.[0] ?? "";
  assert.match(board, /collapsedStages, setCollapsedStages/);
  assert.match(board, /new Set\(RAIL_STAGES\)/);
  assert.match(board, /new Set\(\[\.\.\.current, key\]\)/);
  assert.match(board, /next\.delete\(key\)/);
});

test("direct create uses stage and remains accessible", async () => {
  const source = await readFile(pageUrl, "utf8");
  const dialog = await readFile(createDialogUrl, "utf8");
  assert.match(source, /setCreateApplicationOpen\(true\)[\s\S]*?新增岗位/);
  assert.match(dialog, /role="dialog"/);
  assert.match(dialog, /stage,/);
  assert.match(dialog, /next_action:/);
  assert.match(dialog, /完成后阶段（可不变）/);
  assert.match(dialog, /请先填写下一步/);
  assert.match(dialog, /nextStageAfterCurrentStageChange/);
  assert.match(dialog, /这项安排会保留在当前表单中/);
  assert.match(dialog, /nextStageManuallyEdited/);
  assert.match(dialog, /setNextStageManuallyEdited\(true\)/);
  assert.doesNotMatch(dialog, /setNextStep\(""\)/);
  assert.match(dialog, /applied_date: appliedDate \|\| null/);
  assert.match(dialog, /留空会按产品时区自动记为今天/);
  assert.match(dialog, /pause_reason: stage === "pooled"/);
  assert.match(dialog, /fieldset disabled=\{saving\}/);
  assert.match(dialog, /disabled=\{!nextDate\}/);
  assert.doesNotMatch(dialog, /status,/);
});

test("board cards keep stage movement on drag without a footer selector", async () => {
  const source = await readFile(pageUrl, "utf8");
  const board = source.match(/function TimelineBoard[\s\S]*?\n}\n/)?.[0] ?? "";
  assert.match(board, /onDragStart=\{\(event\) => startDrag\(event, item\)\}/);
  assert.doesNotMatch(board, /移动至/);
  assert.doesNotMatch(board, /aria-label=\{`移动\$\{item\.company\}/);
  assert.match(board, /const notice = notices\[item\.id\]/);
  assert.doesNotMatch(board, /pointer-events-none absolute inset-0 z-10/);
  assert.match(board, /ml-1 inline-flex shrink-0 items-center gap-1 rounded-md border/);
  assert.match(board, /"border-bad\/20 bg-bad\/10 text-bad"[\s\S]*?"border-ok\/20 bg-ok\/10 text-ok"[\s\S]*?"border-info\/20 bg-info\/10 text-info"/);
  assert.match(source, /kind: "saved",\s*message: l\("已保存", "Saved"\)/);
  assert.match(source, /kind: "error",\s*message: l\("失败请重试", "Failed—try again"\)/);
  assert.match(board, /notice\?\.kind === "error" \? "×" : notice\?\.kind === "saved" \? "✓"/);
  const card = source.split("function renderCard(item: BoardItem)")[1]?.split("const columnDot")[0] ?? "";
  assert.match(card, /relative shrink-0 overflow-hidden/);
  assert.match(card, /items-center gap-1 px-2 py-2 text-left/);
  assert.match(card, /min-w-0 flex-1 truncate[\s\S]*?ml-1 inline-flex shrink-0/);
  assert.match(card, /tag shrink-0 px-1 py-0 text-\[10px\]/);
  assert.match(card, /item\.priority[\s\S]*?PRIORITY_META\[item\.priority\][\s\S]*?min-w-0 flex-1 truncate text-\[13px\][\s\S]*?font-semibold">\{item\.company\}<\/span>[\s\S]*?text-ink-3"> · <\/span>[\s\S]*?text-ink-2">\{item\.position\}<\/span>/);
  assert.doesNotMatch(card, /flex-col gap-1|block truncate text-xs/);
  assert.doesNotMatch(card, /boardCardMeta|meta\.when|meta\.what/);
  assert.match(board, /items-stretch gap-2 overflow-x-auto/);
  assert.match(board, /rounded-xl border p-1\.5 transition-colors/);
});

test("statistics replace the subtitle with a designed modal backed by real aggregates", async () => {
  const source = await readFile(pageUrl, "utf8");
  assert.match(source, /l\("数据统计", "Statistics"\)/);
  assert.doesNotMatch(source, /interviewingCount/);
  assert.match(source, /getJson<unknown>\("\/api\/timeline\/statistics"/);
  assert.match(source, /role="dialog" aria-modal="true" aria-labelledby="timeline-statistics-title"/);
  assert.match(source, /l\("阶段进展", "Stage progression"\)/);
  assert.match(source, /interview_conversion_percent/);
  assert.match(source, /sm:grid-cols-3/);
  assert.match(source, /\[l\("进行中", "Active"\), statistics\.active_processes[\s\S]*?\[l\("面试", "Interviews"\), statistics\.funnel\.interviewing[\s\S]*?\["Offer", statistics\.offers/);
  assert.match(source, /`面试转化 \$\{formatPercent\(statistics\.interview_conversion_percent\)\}`/);
  assert.doesNotMatch(source, /\["有效申请", statistics\.submitted/);
  assert.match(source, /已挂\/不再跟进/);
  assert.match(source, /statistics\.rejected \+ statistics\.withdrawn/);
  assert.match(source, /STAGE_DOT\.applied[\s\S]*?STAGE_DOT\.written_test[\s\S]*?STAGE_DOT\.interviewing[\s\S]*?STAGE_DOT\.offer[\s\S]*?STAGE_DOT\.rejected/);
  assert.doesNotMatch(source, /statistics\.pooled<\/strong>/);
  assert.match(source, /l\("\* 转化率以“有效申请”为分母，同一岗位只计算一次。"/);
  assert.match(source, /border-info\/15 bg-info-soft text-info/);
});

test("detail save stays actionable and explains validation failures after click", async () => {
  const source = await readFile(pageUrl, "utf8");
  const saveButton = source.match(/onClick=\{\(\) => void saveDetailEdit\(\)\}[\s\S]*?保存更改/)?.[0] ?? "";
  assert.match(saveButton, /disabled=\{profileSaving \|\| noteSaving \|\| historySaving\}/);
  assert.doesNotMatch(saveButton, /nextActionDraftInvalid|historyDraft !== null|noteConflict/);
  const save = source.match(/async function saveDetailEdit\(\)[\s\S]*?\n  }\n\n  async function prepareDetailDelete/)?.[0] ?? "";
  assert.match(save, /岗位备注不能超过 2000 字/);
  assert.match(save, /请先在备注区域选择合并方式/);
  assert.match(save, /请先保存或取消新添加的历程/);
});

test("list mode starts with explicit aligned column labels", async () => {
  const source = await readFile(pageUrl, "utf8");
  assert.match(
    source,
    /l\("公司", "Company"\)[\s\S]*?l\("岗位", "Role"\)[\s\S]*?l\("调研状态", "Research"\)[\s\S]*?l\("当前环节", "Current step"\)[\s\S]*?l\("下一步日期", "Next date"\)[\s\S]*?l\("阶段", "Stage"\)/,
  );
});

test("detail actions, notes, and research status use the compact requested layout", async () => {
  const source = await readFile(pageUrl, "utf8");
  assert.doesNotMatch(source, /aria-label="更多操作"/);
  assert.doesNotMatch(source, /detailMenuOpen/);
  assert.match(source, /\["adaptation", l\("简历优化", "Resume adaptation"\)\][\s\S]*?ml-auto[\s\S]*?l\("编辑申请", "Edit application"\)[\s\S]*?deletePreparing \? l\("准备中…", "Preparing…"\) : l\("删除申请", "Delete application"\)/);
  assert.match(source, /adaptationStatus === "running"[\s\S]*?"bg-info animate-pulse"[\s\S]*?adaptationStatus === "ready"[\s\S]*?"bg-ok"/);
  assert.match(source, /onStatusChange=\{setAdaptationStatus\}/);
  assert.match(source, /detailTab === "adaptation"[\s\S]*?getResumeAdaptation\(selectedId,[\s\S]*?next\.state !== "generation_running" && next\.state !== "research_running"/);
  assert.doesNotMatch(source, /仅存本机/);
  assert.doesNotMatch(source, /noteExpanded/);
  assert.doesNotMatch(source, /detail\.application_note \? "编辑" : "添加"/);
  assert.doesNotMatch(source, /我的备注|岗位 JD/);
  assert.match(source, /aria-label=\{l\(`当前环节：\$\{detail\.current_step \|\| "尚未记录"\}，点击修改`/);
  assert.doesNotMatch(source, /tag h-7 min-w-\[4\.75rem\]/);
  assert.match(source, /tag h-7 justify-center disabled:cursor-not-allowed/);
  assert.match(source, /tag h-7 \$\{STAGE_STYLES/);
  assert.match(source, /l\("部门：", "Department:"\)[\s\S]*?font-medium text-ink-2">\{detail\.department\}<\/span>/);
  assert.match(source, /l\("渠道：", "Channel:"\)[\s\S]*?font-medium text-ink-2">\{detail\.channel\}<\/span>/);
  assert.doesNotMatch(source, /部门 · \{detail\.department\}|渠道 · \{detail\.channel\}/);
  assert.match(source, /detailTab === "overview"[\s\S]*?l\("备注", "Notes"\)[\s\S]*?<ReviewUndoOperationsPanel/);
  assert.match(source, /l\("岗位描述", "Job description"\)/);
  const profileEditor = source.match(/aria-labelledby="job-profile-editor-title"[\s\S]*?<\/fieldset>/)?.[0] ?? "";
  assert.doesNotMatch(profileEditor, />\s*当前阶段\s*</);
  assert.doesNotMatch(profileEditor, /当前环节（可选）/);
  assert.match(source, /flex flex-wrap items-center gap-1 text-xs text-ink-3[\s\S]*?<span className="rounded-md bg-panel-2 px-2 py-1">[\s\S]*?<span aria-hidden="true">→<\/span>[\s\S]*?<span className="rounded-md bg-panel-2 px-2 py-1">/);
  assert.match(source, /还没有备注；点击右上角“编辑”后记录/);
  assert.doesNotMatch(source, /调研生成中：正在联网检索并整理公司与岗位报告/);
  assert.match(source, /调研还在生成中，报告与建议答案完成后会自动补全/);
});

test("priority menu stays inside the drawer and uses concise labels", async () => {
  const source = await readFile(pageUrl, "utf8");
  const priorityControl = source.match(
    /aria-label=\{l\(`优先级：[\s\S]*?\}\s*<\/div>\s*<div className="relative z-20 shrink-0">/,
  )?.[0] ?? "";
  assert.ok(priorityControl);
  assert.match(priorityControl, /absolute left-0 top-full/);
  assert.match(priorityControl, /l\("设置优先级", "Set priority"\)/);
  assert.match(priorityControl, /detail\.priority \? l\(PRIORITY_META\[detail\.priority\]\.label/);
  assert.match(priorityControl, /priority \? l\(PRIORITY_META\[priority\]\.label/);
  assert.doesNotMatch(priorityControl, />未设置</);
  assert.doesNotMatch(priorityControl, /label\}优先级/);
});

test("current step supports click-to-edit and blur-safe CAS autosave", async () => {
  const source = await readFile(pageUrl, "utf8");
  const save = source.match(/async function saveCurrentStep\(\)[\s\S]*?\n  }\n/)?.[0] ?? "";
  assert.ok(save);
  assert.match(source, /onClick=\{editCurrentStep\}/);
  assert.match(source, /aria-label=\{l\("当前环节", "Current step"\)\}/);
  assert.match(source, /onBlur=\{\(\) => void saveCurrentStep\(\)\}/);
  assert.match(source, /event\.key === "Enter"[\s\S]*?event\.currentTarget\.blur\(\)/);
  assert.match(source, /event\.key === "Escape"[\s\S]*?setCurrentStepEditing\(false\)/);
  assert.match(save, /expected_revision: currentDetail\.revision/);
  assert.match(save, /current_step: currentStep \|\| null/);
  assert.match(save, /current_step: loaded\.current_step,[\s\S]*?revision: loaded\.revision/);
  assert.match(save, /status === 409[\s\S]*?await loadDetail\(/);
  assert.match(source, /detailRefreshBlocked = [\s\S]*?currentStepEditing[\s\S]*?currentStepSaving/);
});

test("application detail uses a responsive floating drawer with accessible motion", async () => {
  const [source, stylesheet] = await Promise.all([
    readFile(pageUrl, "utf8"),
    readFile(new URL("../../index.css", import.meta.url), "utf8"),
  ]);
  assert.match(source, /job-detail-layer/);
  assert.match(source, /job-detail-backdrop/);
  assert.match(source, /job-detail-drawer/);
  assert.doesNotMatch(source, /max-w-\[620px\]/);
  assert.match(stylesheet, /\.job-detail-drawer\s*\{[\s\S]*?width:\s*clamp\(760px, 54vw, 1040px\)/);
  assert.match(stylesheet, /max-width:\s*calc\(100vw - 24px\)/);
  assert.match(stylesheet, /@keyframes job-detail-drawer-in/);
  assert.match(stylesheet, /@media \(max-width: 767px\)[\s\S]*?\.job-detail-drawer\s*\{[\s\S]*?width:\s*100%[\s\S]*?max-width:\s*none/);
  assert.match(stylesheet, /@media \(prefers-reduced-motion: reduce\)/);
});

test("top-right edit owns next-action and note editing while failed completion closes atomically", async () => {
  const source = await readFile(pageUrl, "utf8");
  assert.doesNotMatch(source, /onClick=\{editNextAction\}/);
  assert.doesNotMatch(source, /function rebaseOpenDetailEditAfterPlanWrite/);
  assert.match(source, /detailEditing && nextActionDraft && !stageEndsApplication/);
  assert.match(source, /与其他字段一起由右上角“保存更改”统一保存/);
  assert.match(source, /还没有下一步；点击右上角“编辑”后填写/);
  assert.match(source, /结果设为“未通过”后，这个岗位会移到“已挂”，并清空后续安排/);
  assert.match(source, /completionDraft\?\.outcome === "failed"\s*\? "rejected"/);
  assert.match(source, /outcome === "failed" \? false : completionDraft\.set_next_action/);
});

test("narrow agenda rows wrap long company and action text", async () => {
  const source = await readFile(pageUrl, "utf8");
  assert.match(source, /grid-cols-1[^"]*sm:grid-cols-\[6\.5rem_minmax\(0,1fr\)\]/);
  assert.match(source, /min-w-0 break-words font-medium/);
  assert.match(source, /min-w-0 break-words">\{upcomingActionLabel\(item, locale\)\}/);
});

test("profile terminal changes disclose the exact plan and saving freezes editors", async () => {
  const source = await readFile(pageUrl, "utf8");
  assert.match(source, /stageEndsApplication\(profileDraft\.stage\)/);
  assert.match(source, /formatNextActionForImpact\(detail\.next_action\)/);
  assert.match(source, /fieldset[\s\S]*?disabled=\{profileSaving \|\| noteSaving \|\| historySaving\}/);
  assert.match(source, /fieldset disabled=\{nextActionSaving\}/);
  assert.match(source, /fieldset disabled=\{saving\}/);
});
