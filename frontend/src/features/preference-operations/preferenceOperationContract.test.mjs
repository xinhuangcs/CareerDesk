import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  isPreferenceUpdateOperation,
  preferenceOperationIntegrityIssue,
} from "./preferenceOperationContract.ts";

const operationId = "00000000-0000-4000-8000-000000000001";
const turnId = "00000000-0000-4000-8000-000000000002";

function operation(overrides = {}) {
  const effects = [
    {
      action: "set",
      key: "不存在",
      outcome: "created",
      before_id: null,
      before_revision: null,
      final_id: 11,
      final_revision: 1,
      current: true,
    },
    {
      action: "set",
      key: "工作方式",
      outcome: "updated",
      before_id: 12,
      before_revision: 2,
      final_id: 12,
      final_revision: 3,
      current: true,
    },
    {
      action: "delete",
      key: "旧薪资",
      outcome: "deleted",
      before_id: 13,
      before_revision: 4,
      final_id: null,
      final_revision: null,
      current: true,
    },
    {
      action: "set",
      key: "目标城市",
      outcome: "unchanged",
      before_id: 14,
      before_revision: 5,
      final_id: 14,
      final_revision: 5,
      current: true,
    },
    {
      action: "delete",
      key: "目标方向",
      outcome: "missing",
      before_id: null,
      before_revision: null,
      final_id: null,
      final_revision: null,
      current: true,
    },
  ];
  return {
    operation_id: operationId,
    operation_type: "preference_update",
    contract_version: 1,
    state: "completed",
    created_time: "2026-07-14T10:00:00+00:00",
    client_turn_id: turnId,
    effects,
    current: true,
    result: {
      requested_count: 5,
      changed_count: 3,
      unchanged_count: 1,
      created_count: 1,
      updated_count: 1,
      deleted_count: 1,
      missing_count: 1,
    },
    ...overrides,
  };
}

test("a canonical preference update receipt is accepted", () => {
  const value = operation();
  assert.equal(isPreferenceUpdateOperation(value), true);
  assert.equal(preferenceOperationIntegrityIssue(value), null);
});

test("strict receipts reject unknown fields and anything resembling a stored value", () => {
  assert.equal(isPreferenceUpdateOperation(operation({ value: "secret" })), false);
  const nested = operation();
  nested.effects[0] = { ...nested.effects[0], new_value: "secret" };
  assert.equal(isPreferenceUpdateOperation(nested), false);
  const result = operation();
  result.result = { ...result.result, total_chars: 99 };
  assert.equal(isPreferenceUpdateOperation(result), false);
});

test("UUID, timestamp, effect count, unique key and key length bounds are enforced", () => {
  assert.equal(isPreferenceUpdateOperation(operation({ operation_id: "not-a-uuid" })), false);
  assert.equal(isPreferenceUpdateOperation(operation({ created_time: "not-a-time" })), false);
  assert.equal(isPreferenceUpdateOperation(operation({ created_time: "2026-07-14" })), false);
  assert.equal(
    isPreferenceUpdateOperation(operation({ created_time: "2026-07-14T12:00:00+02:00" })),
    false,
  );
  assert.equal(isPreferenceUpdateOperation(operation({ effects: [] })), false);
  const tooMany = operation();
  tooMany.effects = Array.from({ length: 21 }, (_, index) => ({
    ...tooMany.effects[0],
    key: `偏好${index}`,
    final_id: index + 1,
  }));
  tooMany.result = {
    requested_count: 20,
    changed_count: 20,
    unchanged_count: 0,
    created_count: 20,
    updated_count: 0,
    deleted_count: 0,
    missing_count: 0,
  };
  assert.equal(isPreferenceUpdateOperation(tooMany), false);
  const duplicate = operation();
  duplicate.effects[1] = { ...duplicate.effects[1], key: duplicate.effects[0].key };
  assert.equal(isPreferenceUpdateOperation(duplicate), false);
  const unsorted = operation();
  unsorted.effects = [...unsorted.effects].reverse();
  assert.equal(isPreferenceUpdateOperation(unsorted), false);
  const longKey = operation();
  longKey.effects[0] = { ...longKey.effects[0], key: "城".repeat(101) };
  assert.equal(isPreferenceUpdateOperation(longKey), false);
});

test("timestamps exactly match backend canonical UTC datetime.isoformat output", () => {
  for (const createdTime of [
    "2026-07-14T10:00:00+00:00",
    "2026-07-14T10:00:00.123456+00:00",
    "2024-02-29T23:59:59.000001+00:00",
  ]) {
    assert.equal(
      isPreferenceUpdateOperation(operation({ created_time: createdTime })),
      true,
      createdTime,
    );
  }
  for (const createdTime of [
    "2026-07-14T10:00:00Z",
    "2026-07-14T10:00:00+02:00",
    "2026-07-14T10:00:00.1+00:00",
    "2026-07-14T10:00:00.12345+00:00",
    "2026-07-14T10:00:00.000000+00:00",
    "2026-02-30T10:00:00+00:00",
    "0000-01-01T00:00:00+00:00",
  ]) {
    assert.equal(
      isPreferenceUpdateOperation(operation({ created_time: createdTime })),
      false,
      createdTime,
    );
  }
});

test("outcome-specific row identity and revision invariants fail closed", () => {
  const created = operation();
  created.effects[0] = { ...created.effects[0], final_revision: 2 };
  assert.equal(isPreferenceUpdateOperation(created), false);

  const updated = operation();
  updated.effects[1] = { ...updated.effects[1], final_id: 99 };
  assert.equal(isPreferenceUpdateOperation(updated), false);

  const deleted = operation();
  deleted.effects[2] = { ...deleted.effects[2], final_id: 13, final_revision: 5 };
  assert.equal(isPreferenceUpdateOperation(deleted), false);

  const unchanged = operation();
  unchanged.effects[3] = { ...unchanged.effects[3], final_revision: 6 };
  assert.equal(isPreferenceUpdateOperation(unchanged), false);

  const missing = operation();
  missing.effects[4] = { ...missing.effects[4], before_id: 15, before_revision: 1 };
  assert.equal(isPreferenceUpdateOperation(missing), false);
});

test("all aggregate counts must exactly match effects", () => {
  for (const field of [
    "requested_count",
    "changed_count",
    "unchanged_count",
    "created_count",
    "updated_count",
    "deleted_count",
    "missing_count",
  ]) {
    const candidate = operation();
    candidate.result = { ...candidate.result, [field]: candidate.result[field] - 1 };
    assert.equal(isPreferenceUpdateOperation(candidate), false, field);
  }
});

test("a stale historical receipt is valid only when top-level current matches every effect", () => {
  const historical = operation();
  historical.effects[1] = { ...historical.effects[1], current: false };
  historical.current = false;
  assert.equal(isPreferenceUpdateOperation(historical), true);

  const contradictory = operation();
  contradictory.effects[1] = { ...contradictory.effects[1], current: false };
  assert.equal(isPreferenceUpdateOperation(contradictory), false);
});

test("a pure no-op request cannot create an operation receipt", () => {
  const candidate = operation();
  candidate.effects = [candidate.effects[3]];
  candidate.result = {
    requested_count: 1,
    changed_count: 0,
    unchanged_count: 1,
    created_count: 0,
    updated_count: 0,
    deleted_count: 0,
    missing_count: 0,
  };
  assert.equal(isPreferenceUpdateOperation(candidate), false);
});

test("the receipt presentation has no value renderer, undo callback or action button", () => {
  const source = readFileSync(
    new URL("./preferenceOperationPresentation.ts", import.meta.url),
    "utf8",
  );
  assert.doesNotMatch(source, /effect\.value|operation\.value|onUndo|<button/);
  assert.match(source, /偏好历史操作/);
});
