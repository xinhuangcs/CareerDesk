import assert from "node:assert/strict";
import { test } from "node:test";

import {
  cancelChatTurn,
  cancelChatTurnIfAbsent,
  getChatRecoveryScope,
  getChatTurnStatus,
  uploadChatAttachment,
} from "./chatApi.ts";

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

function assertInternalSignal(signal, externalSignal) {
  assert.ok(signal instanceof AbortSignal);
  assert.equal(signal.aborted, false);
  if (externalSignal) assert.notStrictEqual(signal, externalSignal);
}

test("Chat turn and recovery reads are no-store and preserve their payloads", async (context) => {
  const turn = { client_turn_id: "turn", state: "completed", terminal: true };
  const scope = { scope: "a".repeat(64) };
  const responses = [turn, scope];
  const fetchMock = context.mock.method(
    globalThis,
    "fetch",
    async () => jsonResponse(responses.shift()),
  );
  const external = new AbortController();
  const turnId = "id/with space?和中文";

  assert.deepEqual(await getChatTurnStatus(turnId, { signal: external.signal }), turn);
  assert.deepEqual(await getChatRecoveryScope({ signal: external.signal }), scope);

  const [turnUrl, turnInit] = fetchArguments(fetchMock, 0);
  assert.equal(turnUrl, `/api/chat/turns/${encodeURIComponent(turnId)}/status`);
  assertInternalSignal(turnInit.signal, external.signal);
  assert.deepEqual(turnInit, { cache: "no-store", signal: turnInit.signal });

  const [scopeUrl, scopeInit] = fetchArguments(fetchMock, 1);
  assert.equal(scopeUrl, "/api/chat/recovery-scope");
  assertInternalSignal(scopeInit.signal, external.signal);
  assert.notStrictEqual(scopeInit.signal, turnInit.signal);
  assert.deepEqual(scopeInit, { cache: "no-store", signal: scopeInit.signal });
});

test("Chat turn cancellation keeps the trusted empty JSON command", async (context) => {
  const responses = [
    {
      client_turn_id: "turn",
      state: "running",
      terminal: false,
      proposal_operations: [],
    },
    {
      client_turn_id: "turn",
      state: "cancelled",
      terminal: true,
      proposal_operations: [],
    },
  ];
  const fetchMock = context.mock.method(
    globalThis,
    "fetch",
    async () => jsonResponse(responses.shift()),
  );
  const external = new AbortController();
  const turnId = "cancel/id 中文";

  assert.deepEqual(
    await cancelChatTurn(turnId, { signal: external.signal }),
    {
      client_turn_id: "turn",
      state: "running",
      terminal: false,
      proposal_operations: [],
    },
  );
  assert.deepEqual(
    await cancelChatTurnIfAbsent(turnId, { signal: external.signal }),
    {
      client_turn_id: "turn",
      state: "cancelled",
      terminal: true,
      proposal_operations: [],
    },
  );
  const [url, init] = fetchArguments(fetchMock, 0);
  assert.equal(url, `/api/chat/turns/${encodeURIComponent(turnId)}/cancel`);
  assertInternalSignal(init.signal, external.signal);
  assert.deepEqual(init, {
    method: "POST",
    headers: {
      "X-CareerDesk-Request": "1",
      "Content-Type": "application/json",
    },
    body: "{}",
    signal: init.signal,
  });
  const [absentUrl] = fetchArguments(fetchMock, 1);
  assert.equal(
    absentUrl,
    `/api/chat/turns/${encodeURIComponent(turnId)}/cancel-if-absent`,
  );
});

test("Chat attachment upload preserves FormData identity and caller cancellation", async (context) => {
  const response = { status: "ok", kind: "document", filename: "cv.txt", text: "hello" };
  const fetchMock = context.mock.method(globalThis, "fetch", async () => jsonResponse(response));
  const form = new FormData();
  form.append("file", new Blob(["hello"], { type: "text/plain" }), "cv.txt");
  const controller = new AbortController();

  assert.deepEqual(
    await uploadChatAttachment(form, { signal: controller.signal }),
    response,
  );
  const [url, init] = fetchArguments(fetchMock);
  assert.equal(url, "/api/uploads");
  assert.deepEqual(init, {
    method: "POST",
    headers: { "X-CareerDesk-Request": "1" },
    body: form,
    signal: controller.signal,
  });
  assert.strictEqual(init.body, form);
  assert.strictEqual(init.signal, controller.signal);
  assert.equal("Content-Type" in init.headers, false);
});
