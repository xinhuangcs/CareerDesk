import assert from "node:assert/strict";
import test from "node:test";

import {
  detectSystemTimeZone,
  installSystemTimeZoneSync,
  postSystemTimeZone,
  syncSystemTimeZoneBeforeAppStart,
} from "./systemTimezoneSync.ts";

test("system timezone detection returns one bounded IANA identifier", () => {
  const timezone = detectSystemTimeZone();
  assert.equal(typeof timezone, "string");
  assert.ok(timezone.length > 0 && timezone.length <= 128);
  assert.doesNotMatch(timezone, /^UTC[+-]\d/);
});

test("timezone sync uses the protected same-origin JSON endpoint", async () => {
  const originalFetch = globalThis.fetch;
  const calls = [];
  globalThis.fetch = async (...args) => {
    calls.push(args);
    return { ok: true };
  };
  try {
    assert.equal(await postSystemTimeZone("Europe/Copenhagen"), true);
  } finally {
    globalThis.fetch = originalFetch;
  }

  assert.equal(calls.length, 1);
  assert.equal(calls[0][0], "/api/settings/system-timezone");
  assert.equal(calls[0][1].method, "POST");
  assert.equal(calls[0][1].headers["X-CareerDesk-Request"], "1");
  assert.equal(calls[0][1].headers["Content-Type"], "application/json");
  assert.deepEqual(JSON.parse(calls[0][1].body), {
    timezone: "Europe/Copenhagen",
  });
});

test("timezone sync fails quietly when the local backend is unavailable", async () => {
  const originalFetch = globalThis.fetch;
  globalThis.fetch = async () => {
    throw new TypeError("offline");
  };
  try {
    assert.equal(await postSystemTimeZone("America/New_York"), false);
  } finally {
    globalThis.fetch = originalFetch;
  }
});

test("timezone sync rechecks the system zone when the window regains focus", async () => {
  const originalWindow = globalThis.window;
  const originalDocument = globalThis.document;
  const fakeWindow = new EventTarget();
  const fakeDocument = new EventTarget();
  fakeDocument.visibilityState = "visible";
  globalThis.window = fakeWindow;
  globalThis.document = fakeDocument;

  let current = "Asia/Shanghai";
  const calls = [];
  const uninstall = installSystemTimeZoneSync(
    () => current,
    async (timezone) => {
      calls.push(timezone);
      return true;
    },
  );
  try {
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.deepEqual(calls, ["Asia/Shanghai"]);

    current = "America/New_York";
    fakeWindow.dispatchEvent(new Event("focus"));
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.deepEqual(calls, ["Asia/Shanghai", "America/New_York"]);

    fakeWindow.dispatchEvent(new Event("focus"));
    await new Promise((resolve) => setTimeout(resolve, 0));
    assert.deepEqual(calls, ["Asia/Shanghai", "America/New_York"]);
  } finally {
    uninstall();
    globalThis.window = originalWindow;
    globalThis.document = originalDocument;
  }
});

test("startup timezone sync completes before app mounting continues", async () => {
  const originalWindow = globalThis.window;
  globalThis.window = {
    setTimeout,
    clearTimeout,
  };
  let completed = false;
  try {
    await syncSystemTimeZoneBeforeAppStart(
      () => "Europe/Copenhagen",
      async () => {
        await new Promise((resolve) => setTimeout(resolve, 0));
        completed = true;
        return true;
      },
    );
    assert.equal(completed, true);
  } finally {
    globalThis.window = originalWindow;
  }
});
