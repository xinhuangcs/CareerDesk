import assert from "node:assert/strict";
import test from "node:test";

import {
  preferenceItemCommandCancelRequest,
  preferenceItemCommandPutRequest,
  preferenceItemCommandReadRequest,
} from "./preferencesRequest.ts";

const commandId = "00000000-0000-4000-8000-000000000001";
const skeleton = { action: "set", target: { id: 7, revision: 3 } };

test("PUT carries the exact value-bearing command only on the wire", () => {
  const controller = new AbortController();
  const request = preferenceItemCommandPutRequest(
    commandId,
    { ...skeleton, value: "secret sentinel" },
    controller.signal,
  );
  assert.equal(request.url, `/api/preferences/item-commands/${commandId}`);
  assert.equal(request.init.method, "PUT");
  assert.equal(request.init.cache, "no-store");
  assert.equal(request.init.signal, controller.signal);
  assert.deepEqual(JSON.parse(request.init.body), { ...skeleton, value: "secret sentinel" });
  assert.equal(request.init.headers["X-CareerDesk-Request"], "1");
});

test("GET is bodyless and cancel contains only the non-sensitive skeleton", () => {
  const controller = new AbortController();
  const read = preferenceItemCommandReadRequest(commandId, controller.signal);
  assert.equal(read.init.method, undefined);
  assert.equal("body" in read.init, false);
  const cancel = preferenceItemCommandCancelRequest(commandId, skeleton, controller.signal);
  assert.equal(cancel.url, `/api/preferences/item-commands/${commandId}/cancel-if-absent`);
  assert.equal(cancel.init.method, "POST");
  assert.deepEqual(JSON.parse(cancel.init.body), skeleton);
  assert.equal(cancel.init.body.includes("value"), false);
  assert.equal(cancel.init.body.includes("secret"), false);
});
