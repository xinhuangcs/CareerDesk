import assert from "node:assert/strict";
import { test } from "node:test";

import { isPreferencesSnapshot } from "./preferencesContract.ts";
import { preferencesReadRequest } from "./preferencesRequest.ts";

function snapshot(overrides = {}) {
  return {
    items: [
      {
        id: 1,
        key: "工作方式",
        value: "优先远程或混合办公",
        revision: 2,
        created_time: "2026-07-14T10:00:00+00:00",
        updated_time: "2026-07-14T11:00:00.123456+00:00",
      },
      {
        id: 2,
        key: "目标城市",
        value: "哥本哈根",
        revision: 1,
        created_time: "2026-07-14T10:00:00+00:00",
        updated_time: "2026-07-14T10:00:00+00:00",
      },
    ],
    total: 2,
    total_chars: 21,
    recovery_scope: "a".repeat(64),
    ...overrides,
  };
}

test("the authoritative preference snapshot enforces exact sorted metadata", () => {
  assert.equal(isPreferencesSnapshot(snapshot()), true);
  assert.equal(isPreferencesSnapshot(snapshot({ extra: true })), false);
  const itemExtra = snapshot();
  itemExtra.items[0] = { ...itemExtra.items[0], extra: true };
  assert.equal(isPreferencesSnapshot(itemExtra), false);
  const unsorted = snapshot();
  unsorted.items.reverse();
  assert.equal(isPreferencesSnapshot(unsorted), false);
});

test("count, Unicode character budget, lengths and revisions fail closed", () => {
  assert.equal(isPreferencesSnapshot(snapshot({ total: 1 })), false);
  assert.equal(isPreferencesSnapshot(snapshot({ total_chars: 20 })), false);
  const invalidRevision = snapshot();
  invalidRevision.items[0] = { ...invalidRevision.items[0], revision: 0 };
  assert.equal(isPreferencesSnapshot(invalidRevision), false);
  const longValue = snapshot();
  longValue.items[0] = { ...longValue.items[0], value: "值".repeat(2_001) };
  assert.equal(isPreferencesSnapshot(longValue), false);
  const paddedValue = snapshot();
  paddedValue.items[0] = { ...paddedValue.items[0], value: " 带空格" };
  assert.equal(isPreferencesSnapshot(paddedValue), false);

  const unicode = {
    items: [{
      id: 3,
      key: "方向😀",
      value: "AI🚀",
      revision: 1,
      created_time: "2026-07-14T10:00:00+00:00",
      updated_time: "2026-07-14T10:00:00+00:00",
    }],
    total: 1,
    total_chars: 6,
    recovery_scope: "b".repeat(64),
  };
  assert.equal(isPreferencesSnapshot(unicode), true);

  const invalidId = snapshot();
  invalidId.items[0] = { ...invalidId.items[0], id: 0 };
  assert.equal(isPreferencesSnapshot(invalidId), false);
  const duplicateId = snapshot();
  duplicateId.items[1] = { ...duplicateId.items[1], id: duplicateId.items[0].id };
  assert.equal(isPreferencesSnapshot(duplicateId), false);
  assert.equal(isPreferencesSnapshot(snapshot({ recovery_scope: "A".repeat(64) })), false);
  assert.equal(isPreferencesSnapshot(snapshot({ recovery_scope: "a".repeat(63) })), false);
});

test("snapshot timestamps exactly match canonical backend UTC timestamps", () => {
  for (const updatedTime of [
    "2026-07-14T10:00:00+00:00",
    "2026-07-14T10:00:00.123456+00:00",
    "2024-02-29T23:59:59.000001+00:00",
  ]) {
    const candidate = snapshot();
    candidate.items[0] = { ...candidate.items[0], updated_time: updatedTime };
    assert.equal(isPreferencesSnapshot(candidate), true, updatedTime);
  }
  for (const updatedTime of [
    "2026-07-14T10:00:00Z",
    "2026-07-14T10:00:00-01:00",
    "2026-07-14T10:00:00.1+00:00",
    "2026-07-14T10:00:00.12345+00:00",
    "2026-07-14T10:00:00.000000+00:00",
    "2026-02-30T10:00:00+00:00",
    "0000-01-01T00:00:00+00:00",
  ]) {
    const candidate = snapshot();
    candidate.items[0] = { ...candidate.items[0], updated_time: updatedTime };
    assert.equal(isPreferencesSnapshot(candidate), false, updatedTime);
  }
});

test("the preferences settings API is a read-only no-store request", () => {
  const controller = new AbortController();
  const request = preferencesReadRequest(controller.signal);
  assert.equal(request.url, "/api/preferences");
  assert.equal(request.init.cache, "no-store");
  assert.equal(request.init.signal, controller.signal);
  assert.equal("method" in request.init, false);
  assert.equal("body" in request.init, false);
});
