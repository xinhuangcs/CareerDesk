import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";

import {
  clearProposalRecovery,
  forgetProposalOperation,
  forgetReviewProposalTurn,
  proposalOperationFromServer,
  readProposalRecovery,
  readSettledProposalOperations,
  readVisibleTrustedOperationTurns,
  rememberProposalOperation,
  rememberReviewProposalTurn,
  rememberSettledProposalOperation,
  storeVisibleTrustedOperationTurns,
} from "./chatProposalRecovery.ts";

class MemoryStorage {
  items = new Map();

  getItem(key) {
    return this.items.get(key) ?? null;
  }

  setItem(key, value) {
    this.items.set(key, String(value));
  }

  removeItem(key) {
    this.items.delete(key);
  }
}

const scopeA = "a".repeat(64);
const scopeB = "b".repeat(64);
const operationA = "00000000-0000-4000-8000-000000000001";
const operationB = "00000000-0000-4000-8000-000000000002";
const turnA = "00000000-0000-4000-8000-000000000101";
const turnB = "00000000-0000-4000-8000-000000000102";

beforeEach(() => {
  globalThis.window = {
    localStorage: new MemoryStorage(),
    sessionStorage: new MemoryStorage(),
  };
});

test("exact pending operations and Review turn IDs survive same-tab refresh and settle independently", () => {
  const merge = { surface: "application_merge", operationId: operationA };
  const deletion = { surface: "application_delete", operationId: operationB };
  rememberProposalOperation(scopeA, merge);
  rememberProposalOperation(scopeA, deletion);
  rememberProposalOperation(scopeA, merge);
  rememberReviewProposalTurn(scopeA, turnA);
  rememberReviewProposalTurn(scopeA, turnB);

  assert.deepEqual(readProposalRecovery(scopeA), {
    operations: [deletion, merge],
    reviewTurnIds: [turnA, turnB],
  });

  forgetProposalOperation(scopeA, merge);
  forgetReviewProposalTurn(scopeA, turnA);
  assert.deepEqual(readProposalRecovery(scopeA), {
    operations: [deletion],
    reviewTurnIds: [turnB],
  });
});

test("only an allowlisted surface paired with a strict UUID becomes a proposal operation", () => {
  assert.deepEqual(proposalOperationFromServer("intake", operationA), {
    surface: "intake",
    operationId: operationA,
  });
  assert.equal(proposalOperationFromServer("operation_log", operationA), null);
  assert.equal(proposalOperationFromServer("intake", "not-a-uuid"), null);
  assert.equal(proposalOperationFromServer({}, operationA), null);
});

test("proposal recovery is tenant-scoped, rejects malformed state, and deletes unsafe v2 state", () => {
  globalThis.window.localStorage.setItem(
    "careerdesk.chat.proposalRecovery.v2",
    JSON.stringify({ version: 2, recovery_scope: scopeA, surfaces: ["intake"] }),
  );
  rememberProposalOperation(scopeA, { surface: "intake", operationId: operationA });
  assert.equal(globalThis.window.localStorage.getItem("careerdesk.chat.proposalRecovery.v2"), null);
  assert.notEqual(globalThis.window.sessionStorage.getItem("careerdesk.chat.proposalRecovery.v3"), null);
  assert.deepEqual(readProposalRecovery(scopeB), { operations: [], reviewTurnIds: [] });

  globalThis.window.sessionStorage.setItem("careerdesk.chat.proposalRecovery.v3", "{bad");
  assert.deepEqual(readProposalRecovery(scopeA), {
    operations: [],
    reviewTurnIds: [],
  });
});

test("duplicates, unknown fields, and oversized Review turn recovery fail closed", () => {
  const duplicate = {
    version: 3,
    recovery_scope: scopeA,
    operations: [
      { surface: "intake", operation_id: operationA },
      { surface: "intake", operation_id: operationA },
    ],
    review_turn_ids: [],
  };
  globalThis.window.sessionStorage.setItem(
    "careerdesk.chat.proposalRecovery.v3",
    JSON.stringify(duplicate),
  );
  assert.deepEqual(readProposalRecovery(scopeA), { operations: [], reviewTurnIds: [] });

  const unknownField = {
    ...duplicate,
    operations: [{ surface: "intake", operation_id: operationA, target: "unsafe" }],
  };
  globalThis.window.sessionStorage.setItem(
    "careerdesk.chat.proposalRecovery.v3",
    JSON.stringify(unknownField),
  );
  assert.deepEqual(readProposalRecovery(scopeA), { operations: [], reviewTurnIds: [] });

  const tooManyTurns = Array.from({ length: 129 }, (_, index) => (
    `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`
  ));
  globalThis.window.sessionStorage.setItem(
    "careerdesk.chat.proposalRecovery.v3",
    JSON.stringify({
      version: 3,
      recovery_scope: scopeA,
      operations: [],
      review_turn_ids: tooManyTurns,
    }),
  );
  assert.deepEqual(readProposalRecovery(scopeA), { operations: [], reviewTurnIds: [] });
});

test("proposal recovery accepts 200 operation references and rejects 201", () => {
  const operations = Array.from({ length: 200 }, (_, index) => ({
    surface: "intake",
    operation_id: `00000000-0000-4000-8000-${String(index).padStart(12, "0")}`,
  }));
  globalThis.window.sessionStorage.setItem(
    "careerdesk.chat.proposalRecovery.v3",
    JSON.stringify({
      version: 3,
      recovery_scope: scopeA,
      operations,
      review_turn_ids: [],
    }),
  );
  assert.equal(readProposalRecovery(scopeA).operations.length, 200);

  globalThis.window.sessionStorage.setItem(
    "careerdesk.chat.proposalRecovery.v3",
    JSON.stringify({
      version: 3,
      recovery_scope: scopeA,
      operations: [...operations, {
        surface: "intake",
        operation_id: "00000000-0000-4000-8000-000000000200",
      }],
      review_turn_ids: [],
    }),
  );
  assert.deepEqual(readProposalRecovery(scopeA), { operations: [], reviewTurnIds: [] });
});

test("clear removes current and legacy recovery state", () => {
  rememberProposalOperation(scopeA, { surface: "review_undo", operationId: operationA });
  globalThis.window.localStorage.setItem("careerdesk.chat.proposalRecovery.v2", "legacy");
  assert.equal(clearProposalRecovery(scopeA), true);
  assert.equal(globalThis.window.localStorage.getItem("careerdesk.chat.proposalRecovery.v2"), null);
  assert.deepEqual(readProposalRecovery(scopeA), { operations: [], reviewTurnIds: [] });
});

test("shared localStorage proposal markers are discarded instead of crossing tabs", () => {
  globalThis.window.localStorage.setItem(
    "careerdesk.chat.proposalRecovery.v3",
    JSON.stringify({
      version: 3,
      recovery_scope: scopeA,
      operations: [{ surface: "intake", operation_id: operationA }],
      review_turn_ids: [turnA],
    }),
  );

  assert.deepEqual(readProposalRecovery(scopeA), { operations: [], reviewTurnIds: [] });
  assert.equal(globalThis.window.localStorage.getItem("careerdesk.chat.proposalRecovery.v3"), null);
});

test("settled exact proposals suppress durable rediscovery for the current tab", () => {
  const deletion = { surface: "application_delete", operationId: operationA };
  const merge = { surface: "application_merge", operationId: operationB };
  assert.deepEqual(rememberSettledProposalOperation(scopeA, deletion), [deletion]);
  assert.deepEqual(rememberSettledProposalOperation(scopeA, merge), [deletion, merge]);
  assert.deepEqual(rememberSettledProposalOperation(scopeA, deletion), [merge, deletion]);
  assert.deepEqual(readSettledProposalOperations(scopeA), [merge, deletion]);
  assert.deepEqual(readSettledProposalOperations(scopeB), []);
});

test("malformed settled proposal suppression fails closed without touching pending recovery", () => {
  rememberProposalOperation(scopeA, { surface: "intake", operationId: operationA });
  globalThis.window.sessionStorage.setItem(
    "careerdesk.chat.settledProposalOperations.v1",
    JSON.stringify({
      version: 1,
      recovery_scope: scopeA,
      operations: [{ surface: "intake", operation_id: "not-a-uuid" }],
    }),
  );
  assert.deepEqual(readSettledProposalOperations(scopeA), []);
  assert.deepEqual(readProposalRecovery(scopeA).operations, [
    { surface: "intake", operationId: operationA },
  ]);
});

test("visible compact receipts survive refresh but clear with a new topic", () => {
  assert.equal(storeVisibleTrustedOperationTurns(scopeA, [turnA, turnB]), true);
  assert.deepEqual(readVisibleTrustedOperationTurns(scopeA), [turnA, turnB]);
  assert.deepEqual(readVisibleTrustedOperationTurns(scopeB), []);

  assert.equal(storeVisibleTrustedOperationTurns(scopeA, []), true);
  assert.deepEqual(readVisibleTrustedOperationTurns(scopeA), []);
});

test("invalid and duplicate visible receipt IDs fail closed", () => {
  assert.equal(storeVisibleTrustedOperationTurns(scopeA, ["not-a-uuid"]), false);
  assert.equal(storeVisibleTrustedOperationTurns(scopeA, [turnA, turnA]), false);
  assert.deepEqual(readVisibleTrustedOperationTurns(scopeA), []);
});
