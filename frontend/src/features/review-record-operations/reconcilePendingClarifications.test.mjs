import assert from "node:assert/strict";
import { test } from "node:test";

import {
  reconcilePendingClarificationSnapshot,
  shouldFreezeRetainedClarifications,
} from "./reconcilePendingClarifications.ts";

const tracked = { operation_id: "tracked", client_turn_id: "turn-a", revision: 2 };
const earlierGlobal = { operation_id: "tracked", client_turn_id: "turn-a", revision: 1 };
const globalOnly = { operation_id: "global", client_turn_id: "turn-b", revision: 1 };

test("pending membership never overwrites a payload read after the turn barrier", () => {
  const result = reconcilePendingClarificationSnapshot({
    currentOperations: new Map([[tracked.operation_id, tracked]]),
    previousPendingIds: new Set(),
    loadedPendingOperations: [earlierGlobal, globalOnly],
    allowedTurnIds: new Set(["turn-a"]),
    turnCanonicalOperationIds: new Set([tracked.operation_id]),
  });
  assert.equal(result.operations.get("tracked"), tracked);
  assert.equal(result.operations.has("global"), false);
  assert.deepEqual([...result.pendingIds], ["tracked"]);
});

test("a canonical empty snapshot removes only global-only stale cards", () => {
  const result = reconcilePendingClarificationSnapshot({
    currentOperations: new Map([
      [tracked.operation_id, tracked],
      [globalOnly.operation_id, globalOnly],
    ]),
    previousPendingIds: new Set([tracked.operation_id, globalOnly.operation_id]),
    loadedPendingOperations: [],
    allowedTurnIds: new Set(["turn-a"]),
    turnCanonicalOperationIds: new Set([tracked.operation_id]),
  });
  assert.equal(result.operations.has("tracked"), true);
  assert.equal(result.operations.has("global"), false);
});

test("failed pending membership reads freeze retained clarification actions", () => {
  assert.equal(shouldFreezeRetainedClarifications(false, new Set(["old"])), true);
  assert.equal(shouldFreezeRetainedClarifications(false, new Set()), false);
  assert.equal(shouldFreezeRetainedClarifications(true, new Set(["old"])), false);
});
