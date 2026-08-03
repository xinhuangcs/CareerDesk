import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import {
  cancelStorageMigration,
  claimStorageDisclosure,
  loadStorageLocation,
  requestStorageMigration,
  revealStorageLocation,
} from "./storageApi.ts";

const originalFetch = globalThis.fetch;

afterEach(() => {
  globalThis.fetch = originalFetch;
});

function response() {
  return new Response(JSON.stringify({ data_dir: "/safe/data" }), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

test("storage disclosure is a bodyless read", async () => {
  const calls = [];
  globalThis.fetch = async (...args) => {
    calls.push(args);
    return response();
  };

  assert.deepEqual(await loadStorageLocation(), { data_dir: "/safe/data" });
  assert.deepEqual(calls, [["/api/settings/storage", undefined]]);
});

test("the one-time storage disclosure uses a trusted durable claim", async () => {
  const calls = [];
  globalThis.fetch = async (...args) => {
    calls.push(args);
    return new Response(JSON.stringify({ should_show: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };

  assert.deepEqual(await claimStorageDisclosure(), { should_show: true });
  assert.equal(calls[0][0], "/api/settings/storage-disclosure/claim");
  assert.equal(calls[0][1].method, "POST");
  assert.equal(calls[0][1].headers["X-CareerDesk-Request"], "1");
  assert.deepEqual(JSON.parse(calls[0][1].body), {});
});

test("reveal and migration use trusted exact JSON commands", async () => {
  const calls = [];
  globalThis.fetch = async (...args) => {
    calls.push(args);
    return response();
  };

  await revealStorageLocation("data");
  await requestStorageMigration("/new/data");
  await cancelStorageMigration();

  assert.equal(calls[0][0], "/api/settings/storage/reveal");
  assert.equal(calls[0][1].method, "POST");
  assert.equal(calls[0][1].headers["X-CareerDesk-Request"], "1");
  assert.deepEqual(JSON.parse(calls[0][1].body), { target: "data" });
  assert.equal(calls[1][0], "/api/settings/storage/migration");
  assert.deepEqual(JSON.parse(calls[1][1].body), { destination: "/new/data" });
  assert.equal(calls[2][0], "/api/settings/storage/migration");
  assert.equal(calls[2][1].method, "DELETE");
  assert.equal(calls[2][1].headers["X-CareerDesk-Request"], "1");
});
