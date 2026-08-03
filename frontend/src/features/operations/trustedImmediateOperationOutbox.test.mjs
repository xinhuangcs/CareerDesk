import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";

import {
  markTrustedImmediateOperationTurnDispatched,
  readTrustedImmediateOperationOutbox,
  recoverDispatchedTrustedImmediateOperationTurns,
  selectRetainedRecoveryTurnIds,
  settleTrustedImmediateOperationDispatchedTurn,
  storeTrustedImmediateOperationTurns,
  storeUncertainTrustedImmediateOperationActions,
  trustedImmediateOperationTypeFromServer,
} from "./trustedImmediateOperationOutbox.ts";

class MemoryStorage {
  #items = new Map();

  getItem(key) {
    return this.#items.get(key) ?? null;
  }

  setItem(key, value) {
    this.#items.set(key, String(value));
  }

  removeItem(key) {
    this.#items.delete(key);
  }
}

const turn = "00000000-0000-4000-8000-000000000001";
const operation = "00000000-0000-4000-8000-000000000002";
const command = "00000000-0000-4000-8000-000000000003";
const scopeA = "a".repeat(64);
const scopeB = "b".repeat(64);

function action(operationType = "application_update") {
  return { operationType, operationId: operation, commandId: command, clientTurnId: turn };
}

beforeEach(() => {
  globalThis.window = { sessionStorage: new MemoryStorage() };
});

test("dispatch is synchronous and a reload promotes it to uncertain recovery", () => {
  markTrustedImmediateOperationTurnDispatched(scopeA, turn);
  assert.deepEqual(readTrustedImmediateOperationOutbox(scopeA), {
    turnIds: [],
    uncertainTurnIds: [],
    uncertainActionCommands: [],
    dispatchedTurnIds: [turn],
  });

  const recovered = recoverDispatchedTrustedImmediateOperationTurns(scopeA);
  assert.deepEqual(recovered, {
    turnIds: [turn],
    uncertainTurnIds: [turn],
    uncertainActionCommands: [],
    dispatchedTurnIds: [],
  });
});

test("a read-only outbox still promotes a dispatched marker for this mount", () => {
  const storage = globalThis.window.sessionStorage;
  assert.equal(markTrustedImmediateOperationTurnDispatched(scopeA, turn), true);
  storage.setItem = () => { throw new Error("read only"); };
  storage.removeItem = () => { throw new Error("read only"); };

  assert.deepEqual(recoverDispatchedTrustedImmediateOperationTurns(scopeA), {
    turnIds: [turn],
    uncertainTurnIds: [turn],
    uncertainActionCommands: [],
    dispatchedTurnIds: [],
  });
  assert.deepEqual(readTrustedImmediateOperationOutbox(scopeA), {
    turnIds: [],
    uncertainTurnIds: [],
    uncertainActionCommands: [],
    dispatchedTurnIds: [turn],
  });
});

test("a matching explicit no-execution outcome removes the dispatched turn", () => {
  markTrustedImmediateOperationTurnDispatched(scopeA, turn);
  settleTrustedImmediateOperationDispatchedTurn(scopeA, turn, false, false);
  assert.deepEqual(readTrustedImmediateOperationOutbox(scopeA), {
    turnIds: [],
    uncertainTurnIds: [],
    uncertainActionCommands: [],
    dispatchedTurnIds: [],
  });
});

test("turn and typed action writers preserve each other's minimal UUID state", () => {
  storeTrustedImmediateOperationTurns(scopeA, [turn], [turn]);
  storeUncertainTrustedImmediateOperationActions(scopeA, [action("review_timeline_entry_edit")]);
  assert.deepEqual(readTrustedImmediateOperationOutbox(scopeA), {
    turnIds: [turn],
    uncertainTurnIds: [turn],
    uncertainActionCommands: [action("review_timeline_entry_edit")],
    dispatchedTurnIds: [],
  });

  storeTrustedImmediateOperationTurns(scopeA, [turn], []);
  assert.deepEqual(
    readTrustedImmediateOperationOutbox(scopeA).uncertainActionCommands,
    [action("review_timeline_entry_edit")],
  );
});

test("non-action receipt families cannot be persisted as direct action commands", () => {
  for (const operationType of ["review_record", "preference_update"]) {
    globalThis.window.sessionStorage = new MemoryStorage();
    storeTrustedImmediateOperationTurns(scopeA, [turn], [turn]);
    storeUncertainTrustedImmediateOperationActions(scopeA, [action(operationType)]);
    assert.deepEqual(readTrustedImmediateOperationOutbox(scopeA), {
      turnIds: [turn],
      uncertainTurnIds: [turn],
      uncertainActionCommands: [],
      dispatchedTurnIds: [],
    });
    const raw = globalThis.window.sessionStorage.getItem(
      "careerdesk_trusted_immediate_operation_outbox_v3",
    );
    assert.equal(raw.includes(operationType), false);
    assert.equal(raw.includes(operation), false);
    assert.equal(raw.includes("value"), false);
  }
});

test("only the exact server operation enum marks a trusted immediate family", () => {
  for (const operationType of [
    "application_update",
    "review_timeline_entry_edit",
    "review_record",
    "preference_update",
  ]) {
    assert.equal(trustedImmediateOperationTypeFromServer(operationType), operationType);
  }
  assert.equal(trustedImmediateOperationTypeFromServer("preferences"), null);
  assert.equal(trustedImmediateOperationTypeFromServer("update_application"), null);
  assert.equal(trustedImmediateOperationTypeFromServer("list"), null);
  assert.equal(trustedImmediateOperationTypeFromServer(undefined), null);
  const preferenceListStatus = { tool: "preferences", label: "正在读取偏好…" };
  const preferenceApplyStatus = {
    tool: "preferences",
    label: "正在更新偏好…",
    trusted_operation_type: "preference_update",
  };
  assert.equal(
    trustedImmediateOperationTypeFromServer(preferenceListStatus.trusted_operation_type),
    null,
  );
  assert.equal(
    trustedImmediateOperationTypeFromServer(preferenceApplyStatus.trusted_operation_type),
    "preference_update",
  );
});

test("bounded storage keeps the newest turn instead of silently retaining stale data", () => {
  const turns = Array.from({ length: 129 }, (_, index) => (
    `00000000-0000-4000-8000-${String(index + 1).padStart(12, "0")}`
  ));
  storeTrustedImmediateOperationTurns(scopeA, turns, turns);
  const stored = readTrustedImmediateOperationOutbox(scopeA);
  assert.equal(stored.turnIds.length, 128);
  assert.equal(stored.turnIds[0], turns[1]);
  assert.equal(stored.turnIds.at(-1), turns.at(-1));
  assert.deepEqual(stored.uncertainTurnIds, stored.turnIds);
});

test("a typed unresolved command retains its owning turn before resolved history", () => {
  const resolved = Array.from({ length: 128 }, (_, index) => (
    `00000000-0000-4000-8001-${String(index + 1).padStart(12, "0")}`
  ));
  storeTrustedImmediateOperationTurns(scopeA, [turn, ...resolved], []);
  storeUncertainTrustedImmediateOperationActions(scopeA, [action("review_timeline_entry_edit")]);
  const stored = readTrustedImmediateOperationOutbox(scopeA);
  assert.equal(stored.turnIds.length, 128);
  assert.equal(stored.turnIds.includes(turn), true);
  assert.equal(stored.turnIds.includes(resolved[0]), false);
  assert.equal(stored.turnIds.includes(resolved.at(-1)), true);
  assert.equal(stored.uncertainActionCommands[0].clientTurnId, turn);
  assert.equal(
    stored.uncertainActionCommands[0].operationType,
    "review_timeline_entry_edit",
  );
});

test("bounded storage prioritizes an unresolved action over uncertain and resolved turns", () => {
  const uncertain = Array.from({ length: 128 }, (_, index) => (
    `00000000-0000-4000-8002-${String(index + 1).padStart(12, "0")}`
  ));
  storeTrustedImmediateOperationTurns(scopeA, [turn, ...uncertain], [turn, ...uncertain]);
  storeUncertainTrustedImmediateOperationActions(scopeA, [action("review_timeline_entry_edit")]);
  const stored = readTrustedImmediateOperationOutbox(scopeA);
  assert.equal(stored.turnIds.length, 128);
  assert.equal(stored.turnIds.includes(turn), true);
  assert.equal(stored.uncertainTurnIds.includes(turn), false);
  assert.equal(
    stored.uncertainActionCommands[0].operationType,
    "review_timeline_entry_edit",
  );
});

test("a different authenticated scope atomically drops the old account outbox", () => {
  markTrustedImmediateOperationTurnDispatched(scopeA, turn);
  assert.deepEqual(readTrustedImmediateOperationOutbox(scopeB), {
    turnIds: [],
    uncertainTurnIds: [],
    uncertainActionCommands: [],
    dispatchedTurnIds: [],
  });
  assert.deepEqual(readTrustedImmediateOperationOutbox(scopeA), {
    turnIds: [],
    uncertainTurnIds: [],
    uncertainActionCommands: [],
    dispatchedTurnIds: [],
  });
});

test("mount-equivalent writes do not slide the semantic retention timestamp", () => {
  const originalNow = Date.now;
  try {
    Date.now = () => 1_000_000;
    storeTrustedImmediateOperationTurns(scopeA, [turn], [turn]);
    const before = globalThis.window.sessionStorage.getItem(
      "careerdesk_trusted_immediate_operation_outbox_v3",
    );
    Date.now = () => 2_000_000;
    storeTrustedImmediateOperationTurns(scopeA, [turn], [turn]);
    const after = globalThis.window.sessionStorage.getItem(
      "careerdesk_trusted_immediate_operation_outbox_v3",
    );
    assert.equal(after, before);
  } finally {
    Date.now = originalNow;
  }
});

test("storage write failures and read-back mismatches fail closed", () => {
  const throwing = new MemoryStorage();
  throwing.setItem = () => { throw new Error("quota"); };
  globalThis.window.sessionStorage = throwing;
  assert.equal(markTrustedImmediateOperationTurnDispatched(scopeA, turn), false);

  const mismatching = new MemoryStorage();
  mismatching.setItem = function setItem() {};
  globalThis.window.sessionStorage = mismatching;
  assert.equal(markTrustedImmediateOperationTurnDispatched(scopeA, turn), false);
  assert.equal(storeUncertainTrustedImmediateOperationActions(
    scopeA,
    [action("application_update")],
  ), false);
});

test("chat dispatch and undo both guard network submission behind durable persistence", async () => {
  const { readFile } = await import("node:fs/promises");
  const chatSource = await readFile(new URL("../chat/ChatPage.tsx", import.meta.url), "utf8");
  const panelSource = await readFile(
    new URL("./TrustedImmediateOperationsPanel.tsx", import.meta.url),
    "utf8",
  );
  assert.match(chatSource, /if \(!markTrustedImmediateOperationTurnDispatched\(/);
  assert.match(panelSource, /if \(!updateUncertainActionOperationIds\(/);
  assert.match(panelSource, /撤销尚未提交/);
});

test("retention budget keeps action, then uncertain, then most-recent resolved, in order", () => {
  const retained = selectRetainedRecoveryTurnIds(
    ["a", "b", "c", "d", "e"],
    new Set(["e"]),
    new Set(["c"]),
    3,
  );
  assert.deepEqual(retained, ["c", "d", "e"]);
});

test("retention budget prioritizes newest action turns and starves lower tiers when full", () => {
  const retained = selectRetainedRecoveryTurnIds(
    ["a", "b", "c"],
    new Set(["a", "b", "c"]),
    new Set(),
    2,
  );
  assert.deepEqual(retained, ["b", "c"]);
});
