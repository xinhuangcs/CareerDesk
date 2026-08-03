import assert from "node:assert/strict";
import { beforeEach, test } from "node:test";

import {
  clearPreferenceItemCommandOutbox,
  persistPreferenceItemCommand,
  readPreferenceItemCommandOutbox,
} from "./preferenceItemCommandOutbox.ts";

class MemoryStorage {
  items = new Map();
  getItem(key) { return this.items.get(key) ?? null; }
  setItem(key, value) { this.items.set(key, String(value)); }
  removeItem(key) { this.items.delete(key); }
}

const scopeA = "a".repeat(64);
const scopeB = "b".repeat(64);
const command = {
  commandId: "00000000-0000-4000-8000-000000000001",
  action: "set",
  target: { id: 7, revision: 3 },
  createdAt: 1_000,
};

beforeEach(() => {
  globalThis.window = { sessionStorage: new MemoryStorage() };
});

test("one scope-bound command is synchronously persisted and read back without a value or key", () => {
  const originalNow = Date.now;
  Date.now = () => 2_000;
  try {
    assert.equal(persistPreferenceItemCommand(scopeA, command), true);
    assert.deepEqual(readPreferenceItemCommandOutbox(scopeA), { state: "pending", command });
    const [storageKey, raw] = [...globalThis.window.sessionStorage.items.entries()][0];
    assert.equal(storageKey.endsWith(scopeA), true);
    assert.equal(raw.includes("value"), false);
    assert.equal(raw.includes("key"), false);
    assert.equal(raw.includes("secret sentinel"), false);
  } finally {
    Date.now = originalNow;
  }
});

test("different recovery scopes keep independent commands instead of deleting another account", () => {
  const originalNow = Date.now;
  Date.now = () => 2_000;
  try {
    assert.equal(persistPreferenceItemCommand(scopeA, command), true);
    assert.deepEqual(readPreferenceItemCommandOutbox(scopeB), { state: "empty" });
    assert.deepEqual(readPreferenceItemCommandOutbox(scopeA), { state: "pending", command });
  } finally {
    Date.now = originalNow;
  }
});

test("a second pending command cannot overwrite the first and clear is identity-bound", () => {
  const originalNow = Date.now;
  Date.now = () => 2_000;
  try {
    assert.equal(persistPreferenceItemCommand(scopeA, command), true);
    const other = { ...command, commandId: "00000000-0000-4000-8000-000000000002" };
    assert.equal(persistPreferenceItemCommand(scopeA, other), false);
    assert.equal(clearPreferenceItemCommandOutbox(scopeA, other.commandId), false);
    assert.equal(clearPreferenceItemCommandOutbox(scopeA, command.commandId), true);
    assert.deepEqual(readPreferenceItemCommandOutbox(scopeA), { state: "empty" });
  } finally {
    Date.now = originalNow;
  }
});

test("storage throws and read-back mismatches fail closed", () => {
  globalThis.window.sessionStorage.setItem = () => { throw new Error("quota"); };
  assert.equal(persistPreferenceItemCommand(scopeA, command), false);

  const mismatching = new MemoryStorage();
  mismatching.setItem = function setItem(key) { this.items.set(key, "{}"); };
  globalThis.window.sessionStorage = mismatching;
  assert.equal(persistPreferenceItemCommand(scopeA, command), false);
});

test("malformed same-scope envelopes are surfaced and removed", () => {
  const key = `careerdesk_preference_item_command_outbox_v1_${scopeA}`;
  globalThis.window.sessionStorage.setItem(key, JSON.stringify({
    version: 1,
    recovery_scope: scopeA,
    command_id: command.commandId,
    action: "set",
    target: { id: 7, revision: 3 },
    created_at: Date.now(),
    value: "must never persist",
  }));
  assert.deepEqual(readPreferenceItemCommandOutbox(scopeA), { state: "corrupt" });
  assert.equal(globalThis.window.sessionStorage.getItem(key), null);
});
