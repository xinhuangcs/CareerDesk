import assert from "node:assert/strict";
import { test } from "node:test";

import { withRequestTimeout } from "./requestTimeout.ts";

const TIMEOUT_MS = 12_000;

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((promiseResolve, promiseReject) => {
    resolve = promiseResolve;
    reject = promiseReject;
  });
  return { promise, reject, resolve };
}

async function withControlledTimers(run) {
  const originalSetTimeout = globalThis.setTimeout;
  const originalClearTimeout = globalThis.clearTimeout;
  const timers = [];
  let nextId = 1;

  globalThis.setTimeout = (callback, delay, ...args) => {
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
  };
  globalThis.clearTimeout = (id) => {
    const timer = timers.find((candidate) => candidate.id === id);
    if (timer) timer.cleared = true;
  };

  try {
    return await run({
      fire(index = 0) {
        const timer = timers[index];
        assert.ok(timer, `missing controlled timer ${index}`);
        assert.equal(timer.cleared, false, `controlled timer ${index} was already cleared`);
        assert.equal(timer.fired, false, `controlled timer ${index} was already fired`);
        timer.fired = true;
        timer.callback(...timer.args);
      },
      timers,
    });
  } finally {
    globalThis.setTimeout = originalSetTimeout;
    globalThis.clearTimeout = originalClearTimeout;
  }
}

function trackedExternalSignal({ aborted = false, reason } = {}) {
  let currentAborted = aborted;
  let currentReason = reason;
  const activeListeners = [];
  const added = [];
  const removed = [];

  const signal = {
    get aborted() {
      return currentAborted;
    },
    get reason() {
      return currentReason;
    },
    addEventListener(type, listener, options) {
      added.push({ listener, options, type });
      activeListeners.push({ listener, options, type });
    },
    removeEventListener(type, listener) {
      removed.push({ listener, type });
      const index = activeListeners.findIndex(
        (candidate) => candidate.type === type && candidate.listener === listener,
      );
      if (index !== -1) activeListeners.splice(index, 1);
    },
  };

  return {
    activeListeners,
    added,
    abort(nextReason) {
      if (currentAborted) return;
      currentAborted = true;
      currentReason = nextReason;
      for (const entry of [...activeListeners]) {
        entry.listener.call(signal, { target: signal, type: "abort" });
        if (entry.options?.once) {
          const index = activeListeners.indexOf(entry);
          if (index !== -1) activeListeners.splice(index, 1);
        }
      }
    },
    removed,
    signal,
  };
}

function assertSingleClearedTimer(timers) {
  assert.equal(timers.length, 1);
  assert.equal(timers[0].delay, TIMEOUT_MS);
  assert.equal(timers[0].cleared, true);
}

test("success preserves the value and cleans up the timer and caller listener", async () => {
  await withControlledTimers(async ({ timers }) => {
    const external = trackedExternalSignal();
    const value = { exact: "result" };
    let requestSignal;

    const result = await withRequestTimeout(async (signal) => {
      requestSignal = signal;
      assert.notStrictEqual(signal, external.signal);
      assert.equal(signal.aborted, false);
      return value;
    }, external.signal, "unused timeout message");

    assert.strictEqual(result, value);
    assertSingleClearedTimer(timers);
    assert.equal(external.added.length, 1);
    assert.equal(external.added[0].type, "abort");
    assert.deepEqual(external.added[0].options, { once: true });
    assert.equal(external.removed.length, 1);
    assert.equal(external.removed[0].type, "abort");
    assert.strictEqual(external.removed[0].listener, external.added[0].listener);
    assert.equal(external.activeListeners.length, 0);

    external.abort(new Error("too late"));
    assert.equal(requestSignal.aborted, false, "settled requests must detach the caller signal");
  });
});

test("request failures before the timeout are rethrown by identity", async () => {
  await withControlledTimers(async ({ timers }) => {
    const synchronousFailure = new TypeError("synchronous request failure");
    const asynchronousFailure = new Error("asynchronous request failure");

    await assert.rejects(
      withRequestTimeout(() => {
        throw synchronousFailure;
      }, null, "must not replace the original failure"),
      (reason) => reason === synchronousFailure,
    );
    await assert.rejects(
      withRequestTimeout(
        () => Promise.reject(asynchronousFailure),
        undefined,
        "must not replace the original failure",
      ),
      (reason) => reason === asynchronousFailure,
    );

    assert.equal(timers.length, 2);
    for (const timer of timers) {
      assert.equal(timer.delay, TIMEOUT_MS);
      assert.equal(timer.cleared, true);
      assert.equal(timer.fired, false);
    }
  });
});

test("a pre-aborted caller still invokes the request with the exact abort reason", async () => {
  await withControlledTimers(async ({ timers }) => {
    const callerReason = { source: "caller-before-request" };
    const requestFailure = new Error("request observed the caller abort");
    const external = trackedExternalSignal({ aborted: true, reason: callerReason });
    let calls = 0;

    await assert.rejects(
      withRequestTimeout((signal) => {
        calls += 1;
        assert.equal(signal.aborted, true);
        assert.strictEqual(signal.reason, callerReason);
        return Promise.reject(requestFailure);
      }, external.signal, "must not report timeout"),
      (reason) => reason === requestFailure,
    );

    assert.equal(calls, 1);
    assert.equal(external.added.length, 0);
    assert.equal(external.removed.length, 1);
    assertSingleClearedTimer(timers);
    assert.equal(timers[0].fired, false);
  });
});

test("an in-flight caller abort propagates its reason and is not rewritten as timeout", async () => {
  await withControlledTimers(async ({ timers }) => {
    const callerReason = new DOMException("navigation changed", "AbortError");
    const external = trackedExternalSignal();
    let requestSignal;

    const result = withRequestTimeout((signal) => {
      requestSignal = signal;
      return new Promise((_resolve, reject) => {
        signal.addEventListener("abort", () => reject(signal.reason), { once: true });
      });
    }, external.signal, "must not report timeout");

    external.abort(callerReason);
    await assert.rejects(result, (reason) => reason === callerReason);

    assert.equal(requestSignal.aborted, true);
    assert.strictEqual(requestSignal.reason, callerReason);
    assert.equal(external.activeListeners.length, 0);
    assertSingleClearedTimer(timers);
    assert.equal(timers[0].fired, false);
  });
});

test("the controlled deadline aborts the request and emits a plain caller-owned Error", async () => {
  await withControlledTimers(async ({ fire, timers }) => {
    const timeoutMessage = "  调用方拥有的精确超时文案  ";
    const external = trackedExternalSignal();
    let requestSignal;

    const result = withRequestTimeout((signal) => {
      requestSignal = signal;
      return new Promise((_resolve, reject) => {
        signal.addEventListener("abort", () => reject(signal.reason), { once: true });
      });
    }, external.signal, timeoutMessage);

    assert.equal(timers.length, 1);
    assert.equal(timers[0].delay, TIMEOUT_MS);
    fire();
    await assert.rejects(result, (reason) => {
      assert.ok(reason instanceof Error);
      assert.equal(reason.constructor, Error);
      assert.equal(reason.name, "Error");
      assert.equal(reason.message, timeoutMessage);
      assert.notStrictEqual(reason, requestSignal.reason);
      return true;
    });

    assert.equal(requestSignal.aborted, true);
    assert.equal(requestSignal.reason?.name, "AbortError");
    assert.equal(timers[0].cleared, true);
    assert.equal(external.added.length, 1);
    assert.equal(external.removed.length, 1);
    assert.strictEqual(external.removed[0].listener, external.added[0].listener);
    assert.equal(external.activeListeners.length, 0);
  });
});

test("the timeout message is used verbatim without a shared fallback", async () => {
  await withControlledTimers(async ({ fire, timers }) => {
    const result = withRequestTimeout((signal) => new Promise((_resolve, reject) => {
      signal.addEventListener("abort", () => reject(signal.reason), { once: true });
    }), undefined, "");

    fire();
    await assert.rejects(result, (reason) => reason instanceof Error
      && reason.constructor === Error
      && reason.message === "");
    assert.equal(timers[0].cleared, true);
  });
});

test("a request that ignores the abort may still resolve after the deadline", async () => {
  await withControlledTimers(async ({ fire, timers }) => {
    const pending = deferred();
    const value = { late: "success" };
    let requestSignal;

    const result = withRequestTimeout((signal) => {
      requestSignal = signal;
      return pending.promise;
    }, undefined, "must not replace a late success");

    fire();
    assert.equal(requestSignal.aborted, true);
    pending.resolve(value);
    assert.strictEqual(await result, value);
    assert.equal(timers[0].cleared, true);
  });
});

test("a request that ignores the abort maps a later rejection to the timeout message", async () => {
  await withControlledTimers(async ({ fire, timers }) => {
    const pending = deferred();
    const lateFailure = new Error("late transport failure");
    const timeoutMessage = "超时发生后由调用方解释";

    const result = withRequestTimeout(() => pending.promise, null, timeoutMessage);
    fire();
    pending.reject(lateFailure);

    await assert.rejects(result, (reason) => reason instanceof Error
      && reason.constructor === Error
      && reason.message === timeoutMessage
      && reason !== lateFailure);
    assert.equal(timers[0].cleared, true);
  });
});
