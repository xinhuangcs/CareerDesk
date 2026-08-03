import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import { test } from "node:test";

const panelUrl = new URL("./TrustedImmediateOperationsPanel.tsx", import.meta.url);
const reviewRecordCardUrl = new URL(
  "../review-record-operations/ReviewRecordOperationCard.tsx",
  import.meta.url,
);
const reviewRecordBatchCardUrl = new URL(
  "../review-record-operations/ReviewRecordProposalBatchCard.tsx",
  import.meta.url,
);
const reviewRecordApiUrl = new URL(
  "../review-record-operations/reviewRecordOperationApi.ts",
  import.meta.url,
);
const chatPageUrl = new URL("../chat/ChatPage.tsx", import.meta.url);
const operationAnchorPortalUrl = new URL("./operationAnchorPortal.ts", import.meta.url);
const proposalPanelUrls = [
  new URL("../intake-operations/IntakeOperationsPanel.tsx", import.meta.url),
  new URL("../application-merge-operations/ApplicationMergeOperationsPanel.tsx", import.meta.url),
  new URL("../application-delete-operations/ApplicationDeleteOperationsPanel.tsx", import.meta.url),
  new URL("../review-operations/ReviewUndoOperationsPanel.tsx", import.meta.url),
];
const featureFiles = [
  new URL(
    "../application-update-operations/applicationUpdateOperationPresentation.ts",
    import.meta.url,
  ),
  new URL(
    "../application-update-operations/applicationUpdateOperationContract.ts",
    import.meta.url,
  ),
  new URL(
    "../review-timeline-entry-edit-operations/reviewTimelineEntryEditOperationPresentation.ts",
    import.meta.url,
  ),
  new URL(
    "../review-timeline-entry-edit-operations/reviewTimelineEntryEditOperationContract.ts",
    import.meta.url,
  ),
];
const featureDirectories = [
  new URL("../application-update-operations/", import.meta.url),
  new URL("../review-timeline-entry-edit-operations/", import.meta.url),
];

async function productionTypeScriptFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const entryUrl = new URL(entry.name, directory);
    if (entry.isDirectory()) {
      files.push(...await productionTypeScriptFiles(new URL(`${entry.name}/`, directory)));
    } else if (/\.tsx?$/.test(entry.name) && !/\.test\.tsx?$/.test(entry.name)) {
      files.push(entryUrl);
    }
  }
  return files;
}

function escapeRegExp(value) {
  return value.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

test("trusted immediate coordinator delegates application and review-edit business UI", async () => {
  const [panelSource, ...businessSources] = await Promise.all([
    readFile(panelUrl, "utf8"),
    ...featureFiles.map((file) => readFile(file, "utf8")),
  ]);

  for (const importPath of [
    "../application-update-operations/applicationUpdateOperationPresentation",
    "../application-update-operations/applicationUpdateOperationContract",
    "../review-timeline-entry-edit-operations/reviewTimelineEntryEditOperationPresentation",
    "../review-timeline-entry-edit-operations/reviewTimelineEntryEditOperationContract",
  ]) {
    assert.match(
      panelSource,
      new RegExp(`from\\s+["']${escapeRegExp(importPath)}["']`),
      importPath,
    );
  }

  for (const localDefinition of [
    "applicationUpdateOperationAnnouncement",
    "reviewTimelineEntryEditOperationAnnouncement",
    "isApplicationUpdateOperation",
    "isReviewTimelineEntryEditOperation",
    "isApplicationUpdateUndoCommandStatus",
    "isReviewTimelineEntryEditUndoCommandStatus",
    "applicationUpdateOperationIntegrityIssue",
    "reviewTimelineEntryEditOperationIntegrityIssue",
    "operationIntegrityIssue",
    "reviewTimelineEntryEditIntegrityIssue",
    "projectionValue",
    "reviewTimelineEntryValue",
  ]) {
    assert.doesNotMatch(
      panelSource,
      new RegExp(
        `(?:export\\s+)?(?:function\\s+${localDefinition}\\s*\\(`
        + `|(?:const|let|var)\\s+${localDefinition}\\s*=)`,
      ),
      localDefinition,
    );
  }

  assert.match(businessSources[0], /export function applicationUpdateOperationAnnouncement\s*\(/);
  assert.match(businessSources[1], /export function isApplicationUpdateOperation\s*\(/);
  assert.match(
    businessSources[2],
    /export function reviewTimelineEntryEditOperationAnnouncement\s*\(/,
  );
  assert.match(
    businessSources[3],
    /export function isReviewTimelineEntryEditOperation\s*\(/,
  );
});

test("business operation features cannot depend back on the shared coordinator", async () => {
  const files = (await Promise.all(
    featureDirectories.map((directory) => productionTypeScriptFiles(directory)),
  )).flat();

  for (const file of files) {
    const source = await readFile(file, "utf8");
    assert.doesNotMatch(source, /TrustedImmediateOperationsPanel/, file.pathname);
    assert.doesNotMatch(
      source,
      /from\s+["'](?:[^"']*\/)?operations(?:\/[^"']*)?["']/,
      file.pathname,
    );
  }
});

test("operation results use compact in-context receipts without a global history surface", async () => {
  const source = await readFile(panelUrl, "utf8");

  assert.doesNotMatch(source, /操作记录/);
  assert.doesNotMatch(source, /最近操作|recentOperations|aria-modal="true"/);
  assert.doesNotMatch(source, /className="fixed right-4 top-16/);
  assert.match(source, /aria-label=\{l\("本轮待确认操作", "Operations awaiting confirmation in this turn"\)\}/);
  assert.match(source, /reviewReceiptBatchByOperationId/);
  assert.match(source, /visibleReceiptOperations\.map\(\(operation\) =>/);
  assert.match(source, /visibleClientTurnIdSet\.has\(operation\.client_turn_id\)/);
  assert.match(source, /rounded-xl border px-3 py-2 text-sm/);
  assert.match(source, /const canUndo = isTrustedActionOperation\(operation\)/);
  assert.match(source, /const canSupplement = operation\.operation_type === "review_record"/);
});

test("each settled receipt is portalled to the assistant reply from the same client turn", async () => {
  const [panelSource, chatSource] = await Promise.all([
    readFile(panelUrl, "utf8"),
    readFile(chatPageUrl, "utf8"),
  ]);

  assert.match(
    chatSource,
    /id=\{trustedImmediateOperationReceiptAnchorId\(m\.clientTurnId\)\}/,
  );
  assert.match(
    chatSource,
    /receiptAnchorIdForTurn=\{trustedImmediateOperationReceiptAnchorId\}/,
  );
  assert.match(panelSource, /import \{ createPortal \} from "react-dom"/);
  assert.match(
    panelSource,
    /receiptAnchorForTurn\(operation\.client_turn_id\)[\s\S]*review-batch-\$\{operation\.client_turn_id\}/,
  );
  assert.match(panelSource, /clientTurnId: operation\.client_turn_id/);
  assert.match(
    panelSource,
    /receiptAnchorForTurn\(notice\.clientTurnId\)[\s\S]*createPortal\(receipt, anchor, `review-proposal-outcome-\$\{noticeKey\}`\)/,
  );
});

test("receipts whose stopped turn has no message anchor stay narrow and precede later messages", async () => {
  const source = await readFile(chatPageUrl, "utf8");
  const coordinator = source.indexOf("<TrustedImmediateOperationsPanel");
  const messageList = source.indexOf("{messages.map((m) =>", coordinator);

  assert.notEqual(coordinator, -1);
  assert.notEqual(messageList, -1);
  assert.ok(coordinator < messageList);
  assert.match(
    source.slice(coordinator, messageList),
    /className="max-w-2xl"/,
  );
  assert.match(
    source,
    /const hasConversationContent = hasMessages[\s\S]*trustedOperationTurnIds\.length > 0/,
  );
  assert.match(source, /!hasConversationContent && \(/);
  assert.match(source, /\{hasConversationContent \? \(/);
});

test("every exact pending proposal is portalled beneath its owning assistant reply", async () => {
  const [panelSource, chatSource, portalSource, ...proposalSources] = await Promise.all([
    readFile(panelUrl, "utf8"),
    readFile(chatPageUrl, "utf8"),
    readFile(operationAnchorPortalUrl, "utf8"),
    ...proposalPanelUrls.map((url) => readFile(url, "utf8")),
  ]);

  assert.match(chatSource, /setProposalOperationTurnIds/);
  assert.match(
    chatSource,
    /revealProposalOperation\(proposal, clientTurnId\)/,
  );
  assert.match(chatSource, /discovery\.operation, discovery\.clientTurnId/);
  assert.match(chatSource, /proposalAnchorIdFor\(surface, operationId\)/);
  assert.match(
    panelSource,
    /receiptAnchorForTurn\(batch\.clientTurnId\)[\s\S]*pending-review-proposal-batch-/,
  );
  assert.match(
    panelSource,
    /onReviewUndoPrepared\?\.\(prepared\.operation_id, candidate\.client_turn_id\)/,
  );
  assert.match(panelSource, /allTurnContentAnchored/);
  assert.match(portalSource, /anchorIdForOperation\(operationId\)/);
  assert.match(portalSource, /anchor === null \? content : createPortal\(content, anchor, portalKey\)/);
  for (const source of proposalSources) {
    assert.match(source, /anchorIdForOperation\?: OperationAnchorIdResolver/);
  }
  for (const source of [proposalSources[0], proposalSources[1]]) {
    assert.match(source, /renderOperationAtAnchor\(/);
    assert.match(source, /allOperationContentAnchored/);
    assert.match(source, /\? "contents"/);
  }
  const shellSource = await readFile(
    new URL("./BinaryProposalPanelShell.tsx", import.meta.url),
    "utf8",
  );
  assert.match(shellSource, /renderOperationAtAnchor\(/);
  assert.match(shellSource, /allOperationContentAnchored/);
  assert.match(shellSource, /\? "contents"/);
  for (const source of [proposalSources[2], proposalSources[3]]) {
    assert.match(source, /<BinaryProposalPanelShell</);
    assert.match(source, /anchorIdForOperation=\{anchorIdForOperation\}/);
  }
});

test("ordinary empty chats stay clean while pending decisions and current receipts recover safely", async () => {
  const source = await readFile(chatPageUrl, "utf8");

  assert.match(source, /PROPOSAL_PANEL_SURFACES\.map\(\(surface\)/);
  assert.match(source, /const operationIds = proposalIdsFor\(surface\)/);
  assert.match(source, /const ProposalPanel = PROPOSAL_PANELS\[surface\]/);
  assert.match(source, /operationIds=\{operationIds\}/);
  const registrySource = await readFile(
    new URL("../chat/proposalPanelRegistry.tsx", import.meta.url),
    "utf8",
  );
  assert.match(
    registrySource,
    /Record<\s*ProposalOperation\["surface"\],\s*ComponentType<ProposalPanelProps>\s*>/,
    "the registry must stay type-exhaustive over proposal surfaces",
  );
  for (const panel of [
    "IntakeOperationsPanel",
    "ApplicationMergeOperationsPanel",
    "ApplicationDeleteOperationsPanel",
    "ReviewUndoOperationsPanel",
  ]) {
    assert.match(registrySource, new RegExp(`: ${panel},`), panel);
  }
  assert.match(
    source,
    /trustedOperationRecoveryScope !== null[\s\S]*trustedOperationTurnIds\.length > 0/,
  );
  assert.doesNotMatch(source, /hasMessages && trustedOperationRecoveryScope !== null/);
  assert.match(source, /readProposalRecovery\(response\.scope\)/);
  assert.match(source, /proposalRecovery\.operations/);
  assert.match(source, /proposalRecovery\.reviewTurnIds/);
  assert.match(source, /rememberReviewProposalTurn\(trustedOperationRecoveryScope, clientTurnId\)/);
  assert.match(source, /operationType === "review_record" && trustedOperationRecoveryScope !== null/);
  assert.match(source, /rememberProposalOperation\(trustedOperationRecoveryScope, operation\)/);
  assert.match(source, /forgetProposalOperation\(trustedOperationRecoveryScope, operation\)/);
  assert.match(source, /readSettledProposalOperations\(response\.scope\)/);
  assert.match(source, /settledProposalOperationKeysRef\.current\.has\(key\)/);
  assert.match(source, /rememberSettledProposalOperation\(trustedOperationRecoveryScope, operation\)/);
  assert.match(source, /proposalOperationFromServer\([\s\S]*ev\.data\.proposal_operation_id/);
  assert.match(source, /proposalOperationsFromServer\([\s\S]*ev\.data\.proposal_operations/);
  assert.match(
    source,
    /const keepTrustedOperationCandidate = !explicitlyCancelled && \(completed[\s\S]*\|\| observedTrustedImmediateOperation/,
  );
  assert.match(source, /terminal-empty removes pure reads/);
  assert.match(source, /readVisibleTrustedOperationTurns\(response\.scope\)/);
  assert.match(source, /storeVisibleTrustedOperationTurns\(/);
  assert.match(source, /visibleClientTurnIds=\{visibleTrustedOperationTurnIds\}/);
  assert.match(source, /onProposalOperationsDiscovered=\{revealDiscoveredProposalOperations\}/);
  assert.match(source, /onReviewUndoPrepared=\{\(operationId, clientTurnId\) =>/);
  assert.match(source, /const retainedTrustedTurnIds = \[\.\.\.uncertainTrustedOperationTurnIds\]/);
  assert.match(source, /clearProposalRecovery\(trustedOperationRecoveryScope\)/);
  assert.match(source, /proposalOperationsRef\.current = \[\]/);
  assert.match(source, /await getPendingReviewRecordConfirmations\(\)/);
  assert.match(source, /knownPendingProposals\.size > 0/);
  assert.match(source, /\.\.\.proposalOperationsRef\.current/);
  assert.match(source, /await Promise\.allSettled\([\s\S]*getChatTurnStatus\(clientTurnId\)/);
  assert.match(source, /chatTurnStatusFromServer\(result\.value, clientTurnId\)/);
  assert.match(source, /if \(!status\.terminal\)/);
  assert.match(
    source,
    /revealProposalOperation\(discovery\.operation, discovery\.clientTurnId\)/,
  );
  assert.match(source, /if \(hasPendingReviewProposal\)/);
  assert.match(source, /请先统一确认写入内容或全部不写入/);
  assert.match(source, /disabled=\{busy \|\| clearingTopic\}/);
  assert.match(source, /Boolean\(recoveryScopeError\)/);
  assert.match(source, /正在准备发送…/);
  assert.doesNotMatch(source, /正在建立当前账号的安全恢复通道/);
});

test("interrupted turn recovery is neutral instead of a warning banner", async () => {
  const source = await readFile(panelUrl, "utf8");
  assert.match(source, /当前请求正在安全收尾；页面会自动核对本轮结果/);
  assert.match(source, /rounded-xl bg-panel-2 px-3 py-2 text-sm text-ink-2/);
  assert.doesNotMatch(source, /连接中断后，当前请求可能仍在安全处理/);
});

test("every destructive proposal shows its complete plan before approve or reject", async () => {
  const sources = await Promise.all(proposalPanelUrls.map((url) => readFile(url, "utf8")));
  assert.doesNotMatch(sources[0], /<details open className="border-b border-line">/);
  assert.match(sources[0], /<fieldset[\s\S]*operation\.positions\.map/);
  assert.match(sources[1], /<details open className="border-b border-line">/);
  assert.doesNotMatch(sources[2], /<details/);
  assert.match(sources[2], /<ProposalDecisionCard[\s\S]*删除范围[\s\S]*保留内容/);
  assert.doesNotMatch(sources[2], /max-h-|overflow-y-auto/);
  assert.doesNotMatch(sources[3], /<details/);
  assert.match(sources[3], /撤销内容[\s\S]*?将移除[\s\S]*?撤销后/);
  for (const source of sources) assert.doesNotMatch(source, /Agent 准备/);
});

test("pending Review proposals stay before compact terminal receipts", async () => {
  const source = await readFile(panelUrl, "utf8");
  const proposalRender = source.indexOf("{pendingReviewProposalBatches.map");
  const receiptRender = source.indexOf("{visibleReceiptOperations.length > 0");

  assert.ok(proposalRender >= 0, "pending Review proposals must render");
  assert.ok(receiptRender > proposalRender, "the proposal card must precede compact receipts");
  assert.match(
    source,
    /const pendingReviewProposals = operations\.filter\([\s\S]*operation\.state === "pending_confirmation"/,
  );
  assert.match(
    source,
    /const receiptOperations = operations\.filter\([\s\S]*operation\.state !== "pending_confirmation"/,
  );
  assert.match(source, /groupReviewRecordProposalsByTurn\(\s*pendingReviewProposals/);
  assert.match(source, /<ReviewRecordProposalBatchCard/);
  assert.match(
    source,
    /onClick=\{\(\) => onReviewClarificationRequested\?\.\(\s*operation\.review_reference/,
  );
  assert.match(source, /reviewRecordProposalRecoveryActionsRef/);
  assert.match(source, /REVIEW_RECORD_PROPOSAL_RECOVERY_MS/);
  assert.match(source, /runReviewRecordProposalBatchAction\(batch\.operations, decisions, action\)/);
  assert.match(source, /decideReviewRecordOperationsByClientTurn\(/);
  assert.match(
    source,
    /onReviewProposalSettled\?\.\([\s\S]*settled\.operation_id,[\s\S]*settled\.review_reference,[\s\S]*settled\.client_turn_id/,
  );
  assert.match(source, /for \(const \[operationId, current\] of nextById\)/);
  assert.match(source, /!loadedIds\.has\(operationId\)[\s\S]*current\.state === "pending_confirmation"/);
  assert.match(source, /for \(const previousId of pendingConfirmationOperationIdsRef\.current\)/);
  assert.match(source, /if \(!allowedTurnIds\.has\(candidate\.client_turn_id\)\) continue/);
  assert.match(
    source,
    /onReviewProposalSettled\?\.\([\s\S]*previousId,[\s\S]*settled\.review_reference,[\s\S]*settled\.client_turn_id/,
  );
  assert.match(
    source,
    /uncertainReviewRecordProposalIdsRef\.current\.has\(operationId\)[\s\S]*nextById\.delete\(operationId\)/,
  );
  assert.match(
    source,
    /uncertainReviewRecordProposalIdsRef\.current\.has\(previousId\)[\s\S]*loadedIds\.add\(previousId\)/,
  );
  assert.match(
    source,
    /pendingConfirmationOperationIdsRef\.current\.has\(item\.operation_id\)[\s\S]*uncertainReviewRecordProposalIdsRef\.current\.has\(item\.operation_id\)/,
  );
  assert.match(source, /settledProposalIdsFromTurn/);
  assert.match(source, /submissionRejection: HttpError \| null/);
  assert.match(source, /reason instanceof HttpError/);
  assert.match(
    source,
    /canonical\?\.every\(\(operation\) => operation\.state === "pending_confirmation"\)[\s\S]*submissionRejection !== null/,
  );
  assert.match(source, /本批记录已经变化，这次没有写入/);
  assert.doesNotMatch(source, /const readUncertain = pendingConfirmationReadUncertain/);
  assert.match(
    source,
    /global pending-confirmation endpoint is a discovery\/membership index[\s\S]*const readUncertain = uncertainTurnIds\.has\(batch\.clientTurnId\)/,
  );
});

test("Review batches expose per-item selection and one complete decision command", async () => {
  const [batchSource, apiSource] = await Promise.all([
    readFile(reviewRecordBatchCardUrl, "utf8"),
    readFile(reviewRecordApiUrl, "utf8"),
  ]);

  assert.match(batchSource, /operations\.map\(\(operation\) =>/);
  assert.doesNotMatch(batchSource, /checkboxId|纳入本次处理/);
  assert.match(batchSource, /editing \? t\("正在编辑", "Editing"\) : changed \? t\("继续编辑", "Continue editing"\) : t\("编辑", "Edit"\)/);
  assert.match(batchSource, /buildReviewRecordProposalBatchDecisions\(/);
  assert.match(batchSource, /去除/);
  assert.match(batchSource, /恢复/);
  assert.match(batchSource, /全部不写入/);
  assert.match(batchSource, /确认处理 \$\{counts\.includedCount\} 条/);
  assert.match(batchSource, /已编辑/);
  assert.match(batchSource, /editingOperationIds\.size > 0/);
  assert.match(batchSource, /请先完成 \$\{editingOperationIds\.size\} 项编辑/);
  assert.match(batchSource, /本轮共 \$\{operations\.length\} 条岗位等待确认/);
  assert.doesNotMatch(batchSource, /max-h-\[min\(46vh,32rem\)\]/);
  assert.doesNotMatch(batchSource, /\[scrollbar-gutter:stable\]/);
  assert.match(batchSource, /准备修改岗位/);
  assert.match(batchSource, /准备新增岗位/);
  assert.match(batchSource, /<ProposalDecisionCard/);
  assert.match(apiSource, /"\/api\/reviews\/record-operations"/);
  assert.match(
    apiSource,
    /by-client-turn\/\$\{encodeURIComponent\(clientTurnId\)\}\/decide/,
  );
  assert.match(apiSource, /\{ decisions \}/);
});

test("Review proposal card distinguishes write, retain-draft, reject, and optional supplement", async () => {
  const source = await readFile(reviewRecordCardUrl, "utf8");

  assert.match(source, /item\.field === "company" \|\| item\.field === "position"/);
  assert.doesNotMatch(source, /<details open=\{pendingConfirmation\}/);
  assert.doesNotMatch(source, /查看方案详情与可选信息/);
  assert.doesNotMatch(source, /已识别事实/);
  assert.match(source, /\{!pendingConfirmation && \(\s*<dl/);
  assert.match(source, /targetPlan\.kind === "existing" \? t\("关联现有岗位", "Existing role"\) : t\("新建岗位", "New role"\)/);
  assert.doesNotMatch(source, /rounded-full bg-panel-2[\s\S]{0,180}新建岗位/);
  assert.doesNotMatch(source, /rounded-xl border border-line bg-panel px-3\.5 py-3/);
  assert.match(source, /appearance === "card"[\s\S]*?"space-y-3 border-b border-line bg-panel px-4 py-3 text-xs"/);
  assert.match(source, /确认后的阶段与环节/);
  assert.doesNotMatch(source, /当前状态变化/);
  assert.match(source, /targetPlan\.current_stage/);
  assert.match(source, /targetPlan\.projected_stage/);
  assert.match(source, /targetPlan\.current_step/);
  assert.match(source, /targetPlan\.projected_step/);
  assert.match(source, /targetPlan\.current_next_action/);
  assert.match(source, /targetPlan\.projected_next_action/);
  assert.match(source, /formatNextAction/);
  assert.match(source, /下一步安排/);
  assert.match(source, /本次发生/);
  assert.match(source, /补充信息（可选）/);
  assert.match(source, /✕ 放弃/);
  assert.match(source, /✓ 保留草稿/);
  assert.match(source, /✓ 写入/);
  assert.match(source, /请分别核对已发生事实、确认后的阶段与环节和下一步安排；确认后才会一次性写入/);
  assert.match(source, /只能继续同一动作/);
});

test("destructive proposal cards keep decisions in the header and Review Undo stays concise", async () => {
  const [deleteSource, undoSource] = await Promise.all([
    readFile(proposalPanelUrls[2], "utf8"),
    readFile(proposalPanelUrls[3], "utf8"),
  ]);

  assert.match(
    deleteSource,
    /tone="danger"[\s\S]*?准备删除岗位[\s\S]*?保留岗位[\s\S]*?删除岗位/,
  );
  assert.match(deleteSource, /删除范围/);
  assert.match(deleteSource, /保留内容/);
  assert.doesNotMatch(deleteSource, /function deletionEntryLabel|<details|max-h-|overflow-y-auto/);
  assert.doesNotMatch(deleteSource, /correction: "修正"/);
  assert.match(
    undoSource,
    /bg-bad-soft[\s\S]*?justify-end[\s\S]*?保留复盘[\s\S]*?撤销复盘/,
  );
  assert.match(undoSource, /撤销内容/);
  assert.match(undoSource, /将移除 \$\{effect\.timeline_entries\.length\} 条历程/);
  assert.match(undoSource, /history entries will be removed/);
  assert.match(undoSource, /岗位本身会保留；上方展示的是撤销后的完整状态/);
  assert.doesNotMatch(undoSource, /查看撤销范围与保留内容/);
  assert.doesNotMatch(undoSource, /执行后的精确影响/);
  assert.doesNotMatch(undoSource, /以下岗位手工字段不会自动回退/);
  assert.doesNotMatch(undoSource, /复盘记录号/);
});

test("application update recovery accepts the same bounded batch as the backend", async () => {
  const source = await readFile(panelUrl, "utf8");

  assert.match(source, /const MAX_APPLICATION_UPDATE_RECEIPTS_PER_TURN = 20/);
  assert.match(
    source,
    /operationType: "application_update",[\s\S]*maxReceiptsPerTurn: MAX_APPLICATION_UPDATE_RECEIPTS_PER_TURN/,
  );
});

test("Review recovery accepts the same bounded fifty-item batch as the backend", async () => {
  const source = await readFile(panelUrl, "utf8");

  assert.match(source, /const MAX_REVIEW_RECORD_RECEIPTS_PER_TURN = 50/);
  assert.match(
    source,
    /operationType: "review_record",[\s\S]*maxReceiptsPerTurn: MAX_REVIEW_RECORD_RECEIPTS_PER_TURN/,
  );
  assert.doesNotMatch(
    source,
    /operationType: "review_record",[\s\S]{0,120}maxReceiptsPerTurn: 1/,
  );
});

test("settled Review proposals leave two-second human outcome notices in chat", async () => {
  const source = await readFile(panelUrl, "utf8");

  assert.match(source, /已按确认内容更新求职进展和复盘记录/);
  assert.match(source, /已保留这条复盘草稿，未更新求职进展或题库/);
  assert.match(source, /已放弃这条复盘方案，未写入任何岗位记录/);
  assert.match(source, /reviewProposalOutcomeNotices/);
  assert.match(source, /storeReviewProposalOutcomeNotice/);
  assert.match(source, /window\.setTimeout\(\(\) => \{[\s\S]*2000/);
  assert.doesNotMatch(source, /关闭这条复盘结果提示/);
  assert.match(source, /reviewRecordProposalOutcomeNotice\(settled, locale\)/);
});

test("same-turn Review receipts collapse into one counted batch with whole-batch undo", async () => {
  const [panelSource, undoSource] = await Promise.all([
    readFile(panelUrl, "utf8"),
    readFile(proposalPanelUrls[3], "utf8"),
  ]);

  assert.match(panelSource, /reviewReceiptBatchByOperationId/);
  assert.match(panelSource, /review-batch-\$\{operation\.client_turn_id\}/);
  assert.match(panelSource, /已按确认内容更新 \$\{appliedCount\} 条求职进展和复盘记录/);
  assert.match(panelSource, /prepareReviewRecordUndoBatch\(batchAppliedOperations\)/);
  assert.match(panelSource, /"撤销本批"/);
  assert.match(undoSource, /renderBatchControls=/);
  assert.match(undoSource, /撤销整批（\$\{count\}）/);
});

test("a global-only proposal settled elsewhere gets a neutral non-fabricated notice", async () => {
  const source = await readFile(panelUrl, "utf8");
  const helperStart = source.indexOf("function reviewRecordProposalChangedElsewhereNotice");
  const helperEnd = source.indexOf("export function TrustedImmediateOperationsPanel", helperStart);

  assert.ok(helperStart >= 0 && helperEnd > helperStart);
  const helper = source.slice(helperStart, helperEnd);
  assert.match(helper, /当前页面无法确认具体结果，请以最新岗位记录为准/);
  assert.match(helper, /tone: "uncertain"/);
  assert.doesNotMatch(helper, /已写入|已放弃|已保留|处理成功/);
  assert.match(
    source,
    /settled\.state === "pending_confirmation"[\s\S]*reviewRecordProposalChangedElsewhereNotice\(settled\.client_turn_id, locale\)/,
  );
  assert.match(source, /notice\.tone === "uncertain"[\s\S]*border-info\/25 bg-info-soft text-info/);
});

test("new-topic reset clears terminal UI but retains uncertain proposal recovery intent", async () => {
  const [panelSource, chatSource] = await Promise.all([
    readFile(panelUrl, "utf8"),
    readFile(chatPageUrl, "utf8"),
  ]);

  assert.match(
    chatSource,
    /setTrustedOperationConversationResetSignal\(\(current\) => current \+ 1\)/,
  );
  assert.match(
    chatSource,
    /conversationResetSignal=\{trustedOperationConversationResetSignal\}/,
  );
  assert.match(
    panelSource,
    /uncertainReviewRecordProposalIdsRef\.current\.has\(operation\.operation_id\)/,
  );
  assert.match(panelSource, /setReviewProposalOutcomeNotices\(\{\}\)/);
  assert.match(
    panelSource,
    /\[conversationResetSignal, replaceOperations, uniqueClientTurnIds\]/,
  );
  assert.match(panelSource, /allowedTurnIds\.has\(operation\.client_turn_id\)/);
  assert.doesNotMatch(panelSource, /setRecentOperationsOpen/);
  assert.match(
    panelSource,
    /retainedUncertainProposalIds[\s\S]*reviewRecordProposalRecoveryActionsRef\.current\.keys/,
  );
  assert.match(
    panelSource,
    /runReviewRecordProposalBatchAction\(batch\.operations, decisions, action\)/,
  );
});

test("canonical failed results stay compact and offer direct recheck", async () => {
  const source = await readFile(panelUrl, "utf8");

  assert.match(source, /operation\.state === "failed"/);
  assert.match(source, /const receiptNeedsAttention = \(operation: TrustedImmediateOperation\)/);
  assert.match(source, /const needsAttention = receiptNeedsAttention\(operation\)/);
  assert.match(source, /needsAttention \? "alert" : "status"/);
  assert.match(source, /\{l\("重新核对", "Recheck"\)\}/);
  assert.doesNotMatch(source, /查看最近操作/);
});

test("all proposal surfaces precede the in-context operation coordinator", async () => {
  const source = await readFile(chatPageUrl, "utf8");
  const receiptCoordinator = source.indexOf("<TrustedImmediateOperationsPanel");
  assert.ok(receiptCoordinator >= 0);
  const proposalLoop = source.indexOf("PROPOSAL_PANEL_SURFACES.map((surface)");
  assert.ok(proposalLoop >= 0, "the proposal registry loop must render");
  assert.ok(
    proposalLoop < receiptCoordinator,
    "proposal panels must stay before in-context confirmations",
  );
  assert.match(source, /const settleReviewRecordProposal = useCallback/);
  const settleStart = source.indexOf("const settleReviewRecordProposal = useCallback");
  const boundReferenceGuard = source.indexOf(
    "if (boundReference !== operationId && boundReference !== reviewReference) return;",
    settleStart,
  );
  assert.ok(settleStart >= 0 && boundReferenceGuard > settleStart);
  const settlementPrefix = source.slice(settleStart, boundReferenceGuard);
  assert.match(settlementPrefix, /draftTurnIdRef\.current === clientTurnId/);
  assert.match(
    settlementPrefix,
    /setError\(\(current\) => current\?\.client_turn_id === clientTurnId \? null : current\)/,
  );
  assert.match(source, /boundReference !== operationId && boundReference !== reviewReference/);
  assert.match(source, /onReviewProposalSettled=\{settleReviewRecordProposal\}/);
});
