import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const source = await readFile(new URL("./usePreferenceItemCommands.ts", import.meta.url), "utf8");

test("only GET 404 is classified as an absent command", () => {
  assert.match(source, /reason instanceof HttpError && reason\.status === 404/);
  assert.doesNotMatch(source, /reason instanceof HttpError && reason\.status >= 400/);
});

test("a recovered command polls without replaying its lost value", () => {
  assert.match(source, /canReplay: false/);
  assert.match(source, /void poll\(command, null\)/);
  assert.match(source, /const payload = !command\.canReplay\s*\? null/);
});

test("the first network submission is ordered after durable outbox persistence", () => {
  const submitStart = source.indexOf("const submit = useCallback");
  const persist = source.indexOf("persistPreferenceItemCommand", submitStart);
  const poll = source.indexOf("void poll(command, payload)", persist);
  assert.ok(submitStart >= 0 && persist > submitStart && poll > persist);
});

test("terminal settlement verifies the canonical snapshot and frozen in-memory value before clear", () => {
  const settleStart = source.indexOf("const settle = useCallback");
  const snapshotRead = source.indexOf("getPreferencesSnapshot", settleStart);
  const frozenValueCheck = source.indexOf("current.value !== command.value", snapshotRead);
  const clear = source.indexOf("clearPreferenceItemCommandOutbox", frozenValueCheck);
  assert.ok(settleStart >= 0 && snapshotRead > settleStart);
  assert.ok(frozenValueCheck > snapshotRead && clear > frozenValueCheck);
});

test("cancel-if-absent is user-triggered after the retry budget, never mount-triggered", () => {
  const safeStop = source.indexOf("const safeStop = useCallback");
  const cancel = source.indexOf("cancelPreferenceItemCommandIfAbsent", safeStop);
  assert.ok(safeStop >= 0 && cancel > safeStop);
  assert.equal(source.slice(0, safeStop).includes("cancelPreferenceItemCommandIfAbsent("), false);
});
