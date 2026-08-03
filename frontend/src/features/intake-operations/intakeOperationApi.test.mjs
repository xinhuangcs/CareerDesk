import assert from "node:assert/strict";
import { test } from "node:test";

import {
  approveIntakeOperation,
  getIntakeOperation,
  getPendingIntakeOperations,
  rejectIntakeOperation,
  uploadWorkbookIntake,
} from "./intakeOperationApi.ts";
import { setRuntimeLocale } from "../../shared/api/runtimeLocale.ts";

function jsonResponse(payload) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function fetchArguments(fetchMock, index = 0) {
  assert.ok(fetchMock.mock.calls[index], `missing fetch call ${index}`);
  return fetchMock.mock.calls[index].arguments;
}

function controlledTimers(context) {
  const timers = [];
  let nextId = 1;

  context.mock.method(globalThis, "setTimeout", (callback, delay, ...args) => {
    const timer = {
      args,
      callback,
      cleared: false,
      delay,
      fired: false,
      id: nextId,
    };
    nextId += 1;
    timers.push(timer);
    return timer.id;
  });
  context.mock.method(globalThis, "clearTimeout", (id) => {
    const timer = timers.find((candidate) => candidate.id === id);
    if (timer) timer.cleared = true;
  });

  return {
    fire(index) {
      const timer = timers[index];
      assert.ok(timer, `missing controlled timer ${index}`);
      assert.equal(timer.cleared, false, `controlled timer ${index} was already cleared`);
      assert.equal(timer.fired, false, `controlled timer ${index} was already fired`);
      timer.fired = true;
      timer.callback(...timer.args);
    },
    timers,
  };
}

function assertInternalSignal(signal, externalSignal) {
  assert.ok(signal instanceof AbortSignal);
  assert.equal(signal.aborted, false);
  if (externalSignal) assert.notStrictEqual(signal, externalSignal);
}

test("pending intake operations use a bodyless no-store read and project operations", async (context) => {
  const operations = [{ operation_id: "pending-1", state: "pending" }];
  const envelope = { operations, ignored: "not part of the public result" };
  const fetchMock = context.mock.method(
    globalThis,
    "fetch",
    async () => jsonResponse(envelope),
  );

  assert.deepEqual(await getPendingIntakeOperations(), operations);
  assert.equal(fetchMock.mock.callCount(), 1);
  const [url, init] = fetchArguments(fetchMock);
  assert.equal(url, "/api/timeline/intake-operations/pending");
  assertInternalSignal(init.signal);
  assert.deepEqual(init, { cache: "no-store", signal: init.signal });
  assert.equal("method" in init, false);
  assert.equal("body" in init, false);
});

test("an intake operation read encodes its identity once and preserves the payload", async (context) => {
  const operation = { operation_id: "detail-1", state: "completed", positions: [] };
  const fetchMock = context.mock.method(
    globalThis,
    "fetch",
    async () => jsonResponse(operation),
  );
  const operationId = "id/with space?和中文";

  assert.deepEqual(await getIntakeOperation(operationId), operation);
  const [url, init] = fetchArguments(fetchMock);
  assert.equal(
    url,
    `/api/timeline/intake-operations/${encodeURIComponent(operationId)}`,
  );
  assertInternalSignal(init.signal);
  assert.deepEqual(init, { cache: "no-store", signal: init.signal });
  assert.equal("method" in init, false);
  assert.equal("body" in init, false);
});

test("approve and reject keep their exact trusted JSON write shapes", async (context) => {
  const approved = { operation_id: "approve-result", state: "completed" };
  const rejected = { operation_id: "reject-result", state: "rejected" };
  const responses = [approved, rejected];
  const fetchMock = context.mock.method(
    globalThis,
    "fetch",
    async () => jsonResponse(responses.shift()),
  );
  const operationId = "write/id 中文";

  assert.deepEqual(await approveIntakeOperation(operationId, [1, 3]), approved);
  assert.deepEqual(await rejectIntakeOperation(operationId), rejected);
  assert.equal(fetchMock.mock.callCount(), 2);

  const encodedId = encodeURIComponent(operationId);
  const [approveUrl, approveInit] = fetchArguments(fetchMock, 0);
  assert.equal(approveUrl, `/api/timeline/intake-operations/${encodedId}/approve`);
  assertInternalSignal(approveInit.signal);
  assert.deepEqual(approveInit, {
    method: "POST",
    headers: {
      "X-CareerDesk-Request": "1",
      "Content-Type": "application/json",
    },
    body: JSON.stringify({ exclude_indexes: [1, 3] }),
    signal: approveInit.signal,
  });

  const [rejectUrl, rejectInit] = fetchArguments(fetchMock, 1);
  assert.equal(rejectUrl, `/api/timeline/intake-operations/${encodedId}/reject`);
  assertInternalSignal(rejectInit.signal);
  assert.notStrictEqual(rejectInit.signal, approveInit.signal);
  assert.deepEqual(rejectInit, {
    method: "POST",
    headers: {
      "X-CareerDesk-Request": "1",
      "Content-Type": "application/json",
    },
    body: "{}",
    signal: rejectInit.signal,
  });
});

test("all five intake APIs preserve an external abort reason through their internal signal", async (context) => {
  const { timers } = controlledTimers(context);
  const observedSignals = [];
  const fetchMock = context.mock.method(globalThis, "fetch", async (_url, init) => {
    observedSignals.push(init.signal);
    return new Promise((_resolve, reject) => {
      init.signal.addEventListener("abort", () => reject(init.signal.reason), { once: true });
    });
  });
  const calls = [
    (signal) => getPendingIntakeOperations({ signal }),
    (signal) => getIntakeOperation("read/id", { signal }),
    (signal) => approveIntakeOperation("approve/id", [2], { signal }),
    (signal) => rejectIntakeOperation("reject/id", { signal }),
    (signal) => uploadWorkbookIntake(new File(["x"], "jobs.csv"), { signal }),
  ];

  for (const [index, call] of calls.entries()) {
    const external = new AbortController();
    const callerReason = { index, source: "intake caller" };
    const result = call(external.signal);
    const internalSignal = observedSignals[index];

    assertInternalSignal(internalSignal, external.signal);
    external.abort(callerReason);
    await assert.rejects(result, (reason) => reason === callerReason);
    assert.equal(internalSignal.aborted, true);
    assert.strictEqual(internalSignal.reason, callerReason);
    assert.equal(timers[index].delay, 12_000);
    assert.equal(timers[index].fired, false);
    assert.equal(timers[index].cleared, true);
  }

  assert.equal(fetchMock.mock.callCount(), 5);
});

test("the controlled 12 second deadline emits each localized Intake message", async (context) => {
  setRuntimeLocale("en");
  const { fire, timers } = controlledTimers(context);
  const fetchMock = context.mock.method(globalThis, "fetch", async (_url, init) => (
    new Promise((_resolve, reject) => {
      init.signal.addEventListener("abort", () => reject(init.signal.reason), { once: true });
    })
  ));
  const cases = [
    {
      invoke: () => getPendingIntakeOperations(),
      message: "Loading pending role imports timed out. Try again.",
    },
    {
      invoke: () => getIntakeOperation("status/id"),
      message: "Checking the role import timed out. Keep checking its final state.",
    },
    {
      invoke: () => approveIntakeOperation("approve/id", [1]),
      message: "The role import action timed out; its final state still needs verification.",
    },
    {
      invoke: () => rejectIntakeOperation("reject/id"),
      message: "The role import action timed out; its final state still needs verification.",
    },
    {
      invoke: () => uploadWorkbookIntake(new File(["x"], "jobs.csv")),
      message: "Reading the workbook timed out. Check its size and try again.",
    },
  ];

  for (const [index, entry] of cases.entries()) {
    const result = entry.invoke();
    assert.equal(timers[index].delay, 12_000);
    assert.equal(timers[index].cleared, false);

    fire(index);
    await assert.rejects(result, (reason) => {
      assert.ok(reason instanceof Error);
      assert.equal(reason.constructor, Error);
      assert.equal(reason.name, "Error");
      assert.equal(reason.message, entry.message);
      return true;
    });
    assert.equal(timers[index].cleared, true);
  }

  assert.equal(fetchMock.mock.callCount(), 5);
  assert.deepEqual(
    [...new Set(cases.map(({ message }) => message))],
    [
      "Loading pending role imports timed out. Try again.",
      "Checking the role import timed out. Keep checking its final state.",
      "The role import action timed out; its final state still needs verification.",
      "Reading the workbook timed out. Check its size and try again.",
    ],
  );
});
