import assert from "node:assert/strict";
import { test } from "node:test";
import { startResumeAdaptationResearchPolling } from "./resumeAdaptationResearchPoll.ts";

function manualClock() {
  let nextId = 1;
  const pending = new Map();
  return {
    pending,
    clock: {
      setTimeout(callback) {
        const id = nextId++;
        pending.set(id, callback);
        return id;
      },
      clearTimeout(id) {
        pending.delete(id);
      },
    },
    async runNext() {
      const entry = pending.entries().next().value;
      assert.ok(entry, "expected a scheduled poll");
      const [id, callback] = entry;
      pending.delete(id);
      callback();
      await new Promise((resolve) => setImmediate(resolve));
    },
  };
}

function response(state) {
  return { state };
}

test("research polling survives running states and transient read failures until ready", async () => {
  const timer = manualClock();
  const reads = [
    response("research_running"),
    response("generation_running"),
    null,
    response("research_required"),
    response("ready"),
  ];
  const ready = [];
  const terminal = [];

  startResumeAdaptationResearchPolling({
    intervalMs: 3_000,
    read: async () => reads.shift(),
    onReady: (value) => ready.push(value.state),
    onTerminal: (value) => terminal.push(value.state),
    clock: timer.clock,
  });

  for (let index = 0; index < 5; index += 1) await timer.runNext();

  assert.deepEqual(ready, ["ready"]);
  assert.deepEqual(terminal, []);
  assert.equal(timer.pending.size, 0);
});

test("cancelling research polling removes the pending check", () => {
  const timer = manualClock();
  let reads = 0;
  const cancel = startResumeAdaptationResearchPolling({
    intervalMs: 3_000,
    read: async () => {
      reads += 1;
      return response("ready");
    },
    onReady: () => undefined,
    onTerminal: () => undefined,
    clock: timer.clock,
  });

  cancel();

  assert.equal(timer.pending.size, 0);
  assert.equal(reads, 0);
});

test("cancelling an in-flight research read suppresses callbacks and rescheduling", async () => {
  const timer = manualClock();
  let resolveRead;
  const ready = [];
  const terminal = [];
  const cancel = startResumeAdaptationResearchPolling({
    intervalMs: 3_000,
    read: () => new Promise((resolve) => {
      resolveRead = resolve;
    }),
    onReady: (value) => ready.push(value.state),
    onTerminal: (value) => terminal.push(value.state),
    clock: timer.clock,
  });

  await timer.runNext();
  cancel();
  resolveRead(response("ready"));
  await new Promise((resolve) => setImmediate(resolve));

  assert.deepEqual(ready, []);
  assert.deepEqual(terminal, []);
  assert.equal(timer.pending.size, 0);
});
