import assert from "node:assert/strict";
import { test } from "node:test";

import {
  classifyTrustedImmediateOperationTurn,
  countMissingKnownTrustedImmediateOperations,
  indexKnownTrustedImmediateOperations,
  loadTrustedImmediateOperationTurn,
} from "./trustedImmediateOperationRecovery.ts";

test("durable status is read before all operation families, which then load concurrently", async () => {
  const calls = [];
  let releaseStatus;
  let releaseApplication;
  let releaseReview;
  let releaseRecord;
  let releasePreference;
  const statusGate = new Promise((resolve) => { releaseStatus = resolve; });
  const applicationGate = new Promise((resolve) => { releaseApplication = resolve; });
  const reviewGate = new Promise((resolve) => { releaseReview = resolve; });
  const recordGate = new Promise((resolve) => { releaseRecord = resolve; });
  const preferenceGate = new Promise((resolve) => { releasePreference = resolve; });

  const loading = loadTrustedImmediateOperationTurn(
    async () => {
      calls.push("status:start");
      await statusGate;
      calls.push("status:end");
      return { terminal: true };
    },
    [
      async () => {
        calls.push("application:start");
        await applicationGate;
        calls.push("application:end");
        return [];
      },
      async () => {
        calls.push("review:start");
        await reviewGate;
        calls.push("review:end");
        return [];
      },
      async () => {
        calls.push("record:start");
        await recordGate;
        calls.push("record:end");
        return [];
      },
      async () => {
        calls.push("preference:start");
        await preferenceGate;
        calls.push("preference:end");
        return [];
      },
    ],
  );

  await Promise.resolve();
  assert.deepEqual(calls, ["status:start"]);
  releaseStatus();
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(calls, [
    "status:start",
    "status:end",
    "application:start",
    "review:start",
    "record:start",
    "preference:start",
  ]);
  releaseApplication();
  releaseReview();
  releaseRecord();
  releasePreference();
  const result = await loading;
  assert.equal(result.turnStatus.status, "fulfilled");
  assert.deepEqual(result.operationLists.map((item) => item.status), [
    "fulfilled",
    "fulfilled",
    "fulfilled",
    "fulfilled",
  ]);
});

test("terminal-empty is decided across all four operation families", () => {
  const base = {
    validTurnStatus: true,
    terminal: true,
    turnState: "completed",
    allOperationListsCanonical: true,
    allOperationReceiptsTerminal: true,
    wasUncertain: false,
  };
  assert.equal(classifyTrustedImmediateOperationTurn({
    ...base,
    combinedReceiptCount: 0,
  }), "terminal_empty");
  assert.equal(classifyTrustedImmediateOperationTurn({
    ...base,
    combinedReceiptCount: 1,
  }), "terminal_with_receipts");
  assert.equal(classifyTrustedImmediateOperationTurn({
    ...base,
    allOperationListsCanonical: false,
    combinedReceiptCount: 0,
  }), "pending");
});

test("cancelled turns with any trusted receipt stay pending for contradiction handling", () => {
  assert.equal(classifyTrustedImmediateOperationTurn({
    validTurnStatus: true,
    terminal: true,
    turnState: "cancelled",
    allOperationListsCanonical: true,
    allOperationReceiptsTerminal: false,
    combinedReceiptCount: 1,
    wasUncertain: true,
  }), "cancelled_with_receipts");
});

test("a persisted action is a known receipt and prevents terminal-empty disposal", () => {
  const turnId = "00000000-0000-4000-8000-000000000001";
  const operationId = "00000000-0000-4000-8000-000000000002";
  const knownByTurn = indexKnownTrustedImmediateOperations(
    new Set([turnId]),
    [],
    [{ operationId, clientTurnId: turnId }],
  );
  const missing = countMissingKnownTrustedImmediateOperations(
    knownByTurn.get(turnId) ?? [],
    new Set(),
  );
  assert.equal(missing, 1);
  assert.equal(classifyTrustedImmediateOperationTurn({
    validTurnStatus: true,
    terminal: true,
    turnState: "completed",
    allOperationListsCanonical: missing === 0,
    allOperationReceiptsTerminal: true,
    combinedReceiptCount: 0,
    wasUncertain: true,
  }), "pending");
});

test("a processing record receipt keeps a terminal owner turn in recovery", () => {
  assert.equal(classifyTrustedImmediateOperationTurn({
    validTurnStatus: true,
    terminal: true,
    turnState: "completed",
    allOperationListsCanonical: true,
    allOperationReceiptsTerminal: false,
    combinedReceiptCount: 1,
    wasUncertain: true,
  }), "pending");
});
