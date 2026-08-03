import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import {
  broadcastPreferenceInvalidation,
  subscribePreferenceInvalidation,
} from "./preferenceInvalidation.ts";

const scope = "a".repeat(64);
const original = globalThis.BroadcastChannel;

afterEach(() => {
  globalThis.BroadcastChannel = original;
});

test("broadcast carries only version, type and recovery scope", () => {
  const messages = [];
  class Channel {
    postMessage(value) { messages.push(value); }
    close() {}
  }
  globalThis.BroadcastChannel = Channel;
  assert.equal(broadcastPreferenceInvalidation(scope), true);
  assert.deepEqual(messages, [{ version: 1, type: "invalidate", recovery_scope: scope }]);
  const raw = JSON.stringify(messages);
  assert.equal(raw.includes("value"), false);
  assert.equal(raw.includes("command"), false);
  assert.equal(raw.includes("key"), false);
});

test("broadcast failures never escape a completed command settlement", () => {
  globalThis.BroadcastChannel = class Channel {
    postMessage() { throw new Error("disabled"); }
    close() {}
  };
  assert.equal(broadcastPreferenceInvalidation(scope), false);
});

test("unavailable channel construction falls back without crashing the settings page", () => {
  globalThis.BroadcastChannel = class Channel {
    constructor() { throw new Error("unsupported"); }
  };
  const dispose = subscribePreferenceInvalidation(scope, () => {
    throw new Error("must not run");
  });
  dispose();
  assert.equal(broadcastPreferenceInvalidation(scope), false);
});

test("subscriber accepts only exact same-scope invalidations", () => {
  let handler = null;
  let calls = 0;
  class Channel {
    addEventListener(_name, next) { handler = next; }
    removeEventListener() {}
    close() {}
  }
  globalThis.BroadcastChannel = Channel;
  const dispose = subscribePreferenceInvalidation(scope, () => { calls += 1; });
  handler({ data: { version: 1, type: "invalidate", recovery_scope: scope } });
  handler({ data: { version: 1, type: "invalidate", recovery_scope: "b".repeat(64) } });
  handler({ data: { version: 1, type: "invalidate", recovery_scope: scope, value: "secret" } });
  assert.equal(calls, 1);
  dispose();
});
