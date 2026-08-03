import assert from "node:assert/strict";
import { test } from "node:test";

import { preferenceOperationsByTurnReadRequest } from "./preferenceOperationRequest.ts";

test("preference receipt reads are no-store GETs with no request body", async () => {
  const turnId = "00000000-0000-4000-8000-000000000001";
  const controller = new AbortController();
  const request = preferenceOperationsByTurnReadRequest(turnId, controller.signal);

  assert.equal(request.url, `/api/preferences/operations/by-client-turn/${turnId}`);
  assert.equal("method" in request.init, false);
  assert.equal("body" in request.init, false);
  assert.equal(request.init.cache, "no-store");
  assert.equal(request.init.signal, controller.signal);
});
