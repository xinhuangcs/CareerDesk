import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

import { notifyTerminalOperationOnce } from "./useBinaryTrustedOperationQueue.ts";

test("terminal operation callbacks fire exactly once per canonical operation", () => {
  const known = new Set();
  const settled = [];
  const notify = (operation) => settled.push(`${operation.operation_id}:${operation.state}`);

  assert.equal(notifyTerminalOperationOnce(
    known,
    { operation_id: "one", state: "pending" },
    notify,
  ), false);
  assert.equal(notifyTerminalOperationOnce(
    known,
    { operation_id: "one", state: "completed" },
    notify,
  ), true);
  assert.equal(notifyTerminalOperationOnce(
    known,
    { operation_id: "one", state: "completed" },
    notify,
  ), false);
  assert.equal(notifyTerminalOperationOnce(
    known,
    { operation_id: "two", state: "rejected" },
    notify,
  ), true);
  assert.equal(notifyTerminalOperationOnce(
    known,
    { operation_id: "three", state: "stale" },
    notify,
  ), true);
  assert.deepEqual(settled, ["one:completed", "two:rejected", "three:stale"]);
});

test("a consumer callback failure cannot undo the terminal tombstone", () => {
  const known = new Set();
  assert.equal(notifyTerminalOperationOnce(
    known,
    { operation_id: "safe", state: "completed" },
    () => { throw new Error("consumer failed"); },
  ), true);
  assert.equal(known.has("safe"), true);
  assert.equal(notifyTerminalOperationOnce(
    known,
    { operation_id: "safe", state: "completed" },
    () => undefined,
  ), false);
});

test("an exact proposal queue reads only named IDs while omitted IDs retain global listing", async () => {
  const source = await readFile(
    new URL("./useBinaryTrustedOperationQueue.ts", import.meta.url),
    "utf8",
  );

  assert.match(source, /operationIds\?: readonly string\[\]/);
  assert.match(source, /exactOperationIds\.map\(async \(operationId\) =>/);
  assert.match(source, /apiRef\.current\.get\(operationId\)/);
  assert.match(source, /exactReadResults === null\s*\? await apiRef\.current\.listPending\(\)/);
  assert.match(source, /canonical\.operation_id !== operationId/);
  assert.match(source, /notifyTerminalOperationOnce\([\s\S]*onOperationSettledRef\.current/);
});

test("all proposal panels accept exact IDs and settle by operation ID", async () => {
  const panelUrls = [
    new URL("../intake-operations/IntakeOperationsPanel.tsx", import.meta.url),
    new URL(
      "../application-merge-operations/ApplicationMergeOperationsPanel.tsx",
      import.meta.url,
    ),
    new URL(
      "../application-delete-operations/ApplicationDeleteOperationsPanel.tsx",
      import.meta.url,
    ),
    new URL("../review-operations/ReviewUndoOperationsPanel.tsx", import.meta.url),
  ];
  const sources = await Promise.all(panelUrls.map((url) => readFile(url, "utf8")));

  for (const source of sources) {
    assert.match(source, /operationIds\?: readonly string\[\]/);
    assert.match(source, /onOperationSettled\?: \(operationId: string\) => void/);
    assert.match(source, /onOperationSettled/);
  }
  assert.match(sources[0], /getIntakeOperation\(operationId/);
  assert.match(sources[1], /useBinaryTrustedOperationQueue<[\s\S]*operationIds,/);
  for (const source of [sources[2], sources[3]]) {
    assert.match(source, /<BinaryProposalPanelShell<[\s\S]*operationIds=\{operationIds\}/);
  }
  const shellSource = await readFile(
    new URL("./BinaryProposalPanelShell.tsx", import.meta.url),
    "utf8",
  );
  assert.match(shellSource, /useBinaryTrustedOperationQueue<T>\(\{[\s\S]*operationIds,/);
});

test("same-turn binary proposals can be settled from one fail-closed batch action", async () => {
  const [queueSource, shellSource, deletePanelSource] = await Promise.all([
    readFile(new URL("./useBinaryTrustedOperationQueue.ts", import.meta.url), "utf8"),
    readFile(new URL("./BinaryProposalPanelShell.tsx", import.meta.url), "utf8"),
    readFile(
      new URL(
        "../application-delete-operations/ApplicationDeleteOperationsPanel.tsx",
        import.meta.url,
      ),
      "utf8",
    ),
  ]);

  assert.match(queueSource, /for \(const operation of batch\)/);
  assert.match(queueSource, /if \(!await runAction\(operation, command, true\)\) break/);
  assert.match(shellSource, /operationIds !== undefined && queue\.operations\.length > 1/);
  assert.match(shellSource, /queue\.runBatchAction\("approve"\)/);
  assert.match(deletePanelSource, /全部保留/);
  assert.match(deletePanelSource, /全部删除（\$\{count\}）/);
});

test("binary terminal notices share a two-second non-dismissible lifecycle", async () => {
  const [queueSource, shellSource, mergeSource] = await Promise.all([
    readFile(new URL("./useBinaryTrustedOperationQueue.ts", import.meta.url), "utf8"),
    readFile(new URL("./BinaryProposalPanelShell.tsx", import.meta.url), "utf8"),
    readFile(
      new URL(
        "../application-merge-operations/ApplicationMergeOperationsPanel.tsx",
        import.meta.url,
      ),
      "utf8",
    ),
  ]);

  assert.match(queueSource, /const NOTICE_DURATION_MS = 2000/);
  assert.match(queueSource, /window\.setTimeout\(\(\) => \{[\s\S]*NOTICE_DURATION_MS/);
  assert.doesNotMatch(queueSource, /dismissNotice/);
  assert.doesNotMatch(shellSource, /dismissNotice|aria-label=\{dismissNoticeLabel\}/);
  assert.doesNotMatch(mergeSource, /dismissNotice|关闭这条岗位合并结果提示/);
});
