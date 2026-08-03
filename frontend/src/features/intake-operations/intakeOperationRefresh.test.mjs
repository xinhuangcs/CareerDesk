import assert from "node:assert/strict";
import { test } from "node:test";

import {
  mergePendingIntakeOperations,
  reconcileIntakeExcludedRows,
  retainIntakeOperationErrors,
} from "./intakeOperationRefresh.ts";

function operation(operationId, positionCount) {
  return {
    operation_id: operationId,
    state: "pending",
    positions: Array.from({ length: positionCount }, () => ({})),
  };
}

test("an ordinary pending refresh evicts entries absent from the server list", () => {
  const removed = operation("removed", 3);
  const loaded = operation("loaded", 2);
  const protectedIds = new Set();

  assert.deepEqual(
    mergePendingIntakeOperations([removed], [loaded], protectedIds),
    [loaded],
  );
  assert.deepEqual(
    reconcileIntakeExcludedRows(
      { removed: [1], loaded: [1, 2, 3] },
      [loaded],
      protectedIds,
    ),
    { loaded: [1, 2] },
  );
  assert.deepEqual(
    retainIntakeOperationErrors(
      { removed: "obsolete", loaded: "still visible" },
      [loaded],
      protectedIds,
    ),
    { loaded: "still visible" },
  );
});

test("a protected operation missing from the server list keeps its card state", () => {
  const protectedOperation = operation("protected", 3);
  const removed = operation("removed", 1);
  const loaded = operation("loaded", 2);
  const protectedIds = new Set(["protected"]);
  const excludedRows = { protected: [3, 1], removed: [1], loaded: [2] };

  assert.deepEqual(
    mergePendingIntakeOperations(
      [protectedOperation, removed],
      [loaded],
      protectedIds,
    ),
    [loaded, protectedOperation],
  );
  assert.deepEqual(
    reconcileIntakeExcludedRows(excludedRows, [loaded], protectedIds),
    { loaded: [2], protected: [3, 1] },
  );
  assert.deepEqual(excludedRows, { protected: [3, 1], removed: [1], loaded: [2] });
  assert.deepEqual(
    retainIntakeOperationErrors(
      { protected: "final state unknown", removed: "obsolete", loaded: "visible" },
      [loaded],
      protectedIds,
    ),
    { protected: "final state unknown", loaded: "visible" },
  );
});

test("a protected operation returned by the server is not duplicated and uses fresh row bounds", () => {
  const stale = operation("protected", 4);
  const canonical = operation("protected", 2);
  const protectedIds = new Set(["protected"]);

  const merged = mergePendingIntakeOperations([stale], [canonical], protectedIds);
  assert.deepEqual(merged, [canonical]);
  assert.equal(merged.filter((item) => item.operation_id === "protected").length, 1);
  assert.deepEqual(
    reconcileIntakeExcludedRows(
      { protected: [0, 1, 2, 3, 4] },
      [canonical],
      protectedIds,
    ),
    { protected: [1, 2] },
  );
  assert.deepEqual(
    retainIntakeOperationErrors(
      { protected: "keep until canonical handling clears it", removed: "obsolete" },
      [canonical],
      protectedIds,
    ),
    { protected: "keep until canonical handling clears it" },
  );
});
