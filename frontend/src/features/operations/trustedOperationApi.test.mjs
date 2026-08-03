import assert from "node:assert/strict";
import { test } from "node:test";

import {
  approveApplicationDeleteOperation,
  getApplicationDeleteOperation,
  getPendingApplicationDeleteOperations,
  prepareApplicationDeleteOperation,
  rejectApplicationDeleteOperation,
} from "../application-delete-operations/applicationDeleteOperationApi.ts";
import {
  approveApplicationMergeOperation,
  getApplicationMergeOperation,
  getPendingApplicationMergeOperations,
  rejectApplicationMergeOperation,
} from "../application-merge-operations/applicationMergeOperationApi.ts";
import {
  getApplicationUpdateOperation,
  getApplicationUpdateOperationsByClientTurn,
  getApplicationUpdateUndoCommandStatus,
  undoApplicationUpdateOperation,
} from "../application-update-operations/applicationUpdateOperationApi.ts";
import {
  getReviewTimelineEntryEditOperation,
  getReviewTimelineEntryEditOperationsByClientTurn,
  getReviewTimelineEntryEditUndoCommandStatus,
  undoReviewTimelineEntryEditOperation,
} from "../review-timeline-entry-edit-operations/reviewTimelineEntryEditOperationApi.ts";
import {
  approveReviewRecordOperation,
  decideReviewRecordOperationsByClientTurn,
  getPendingReviewRecordConfirmations,
  getPendingReviewRecordClarifications,
  getReviewRecordOperation,
  getReviewRecordOperationsByClientTurn,
  prepareReviewRecordUndoOperation,
  rejectReviewRecordOperation,
} from "../review-record-operations/reviewRecordOperationApi.ts";
import {
  approveReviewUndoOperation,
  getPendingReviewUndoOperations,
  getReviewUndoOperation,
  prepareTimelineReviewUndoOperation,
  rejectReviewUndoOperation,
} from "../review-operations/reviewUndoOperationApi.ts";

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

function assertInternalRead(init, externalSignal) {
  assert.ok(init.signal instanceof AbortSignal);
  assert.equal(init.signal.aborted, false);
  if (externalSignal) assert.notStrictEqual(init.signal, externalSignal);
  assert.deepEqual(init, { cache: "no-store", signal: init.signal });
}

const WRITE_JSON_HEADERS = {
  "X-CareerDesk-Request": "1",
  "Content-Type": "application/json",
};

test("Application Delete and Merge pending reads preserve envelope projection", async (context) => {
  const operations = [{ operation_id: "pending" }];
  const fetchMock = context.mock.method(
    globalThis,
    "fetch",
    async () => jsonResponse({ operations, ignored: true }),
  );
  const cases = [
    [getPendingApplicationDeleteOperations, "/api/timeline/application-delete-operations/pending"],
    [getPendingApplicationMergeOperations, "/api/timeline/application-merge-operations/pending"],
  ];

  for (const [index, [request, expectedUrl]] of cases.entries()) {
    assert.deepEqual(await request(), operations);
    const [url, init] = fetchArguments(fetchMock, index);
    assert.equal(url, expectedUrl);
    assertInternalRead(init);
  }
});

test("Application Delete and Merge detail/actions preserve encoded resources", async (context) => {
  const fetchMock = context.mock.method(
    globalThis,
    "fetch",
    async () => jsonResponse({ operation_id: "result" }),
  );
  const operationId = "operation/id 中文";
  const cases = [
    {
      base: `/api/timeline/application-delete-operations/${encodeURIComponent(operationId)}`,
      get: getApplicationDeleteOperation,
      approve: approveApplicationDeleteOperation,
      reject: rejectApplicationDeleteOperation,
    },
    {
      base: `/api/timeline/application-merge-operations/${encodeURIComponent(operationId)}`,
      get: getApplicationMergeOperation,
      approve: approveApplicationMergeOperation,
      reject: rejectApplicationMergeOperation,
    },
  ];

  let fetchIndex = 0;
  for (const operation of cases) {
    await operation.get(operationId);
    await operation.approve(operationId);
    await operation.reject(operationId);

    const [readUrl, readInit] = fetchArguments(fetchMock, fetchIndex++);
    assert.equal(readUrl, operation.base);
    assertInternalRead(readInit);
    for (const suffix of ["/approve", "/reject"]) {
      const [url, init] = fetchArguments(fetchMock, fetchIndex++);
      assert.equal(url, `${operation.base}${suffix}`);
      assert.ok(init.signal instanceof AbortSignal);
      assert.equal(init.signal.aborted, false);
      assert.deepEqual(init, {
        method: "POST",
        headers: WRITE_JSON_HEADERS,
        body: "{}",
        signal: init.signal,
      });
    }
  }
});

test("Timeline detail delete prepares one server-frozen proposal", async (context) => {
  const fetchMock = context.mock.method(
    globalThis,
    "fetch",
    async () => jsonResponse({ operation_id: "result" }),
  );

  assert.equal((await prepareApplicationDeleteOperation(42)).operation_id, "result");
  const [url, init] = fetchArguments(fetchMock);
  assert.equal(url, "/api/timeline/applications/42/prepare-delete");
  assert.ok(init.signal instanceof AbortSignal);
  assert.deepEqual(init, {
    method: "POST",
    headers: WRITE_JSON_HEADERS,
    body: "{}",
    signal: init.signal,
  });
});

test("three by-turn reads encode identity, use no-store, and project operations", async (context) => {
  const operations = [{ operation_id: "one" }];
  const fetchMock = context.mock.method(
    globalThis,
    "fetch",
    async () => jsonResponse({ operations, ignored: true }),
  );
  const external = new AbortController();
  const turnId = "turn/id 中文";
  const cases = [
    [
      getApplicationUpdateOperationsByClientTurn,
      `/api/timeline/application-update-operations/by-client-turn/${encodeURIComponent(turnId)}`,
    ],
    [
      getReviewTimelineEntryEditOperationsByClientTurn,
      `/api/reviews/timeline-entry-edit-operations/by-client-turn/${encodeURIComponent(turnId)}`,
    ],
    [
      getReviewRecordOperationsByClientTurn,
      `/api/reviews/record-operations/by-client-turn/${encodeURIComponent(turnId)}`,
    ],
  ];

  for (const [index, [request, expectedUrl]] of cases.entries()) {
    assert.deepEqual(await request(turnId, { signal: external.signal }), operations);
    const [url, init] = fetchArguments(fetchMock, index);
    assert.equal(url, expectedUrl);
    assertInternalRead(init, external.signal);
  }
});

test("three canonical operation reads preserve payload and encoded identity", async (context) => {
  const responses = [
    { operation_id: "application" },
    { operation_id: "timeline-entry-edit" },
    { operation_id: "record" },
  ];
  const fetchMock = context.mock.method(
    globalThis,
    "fetch",
    async () => jsonResponse(responses.shift()),
  );
  const external = new AbortController();
  const operationId = "operation/id 中文";
  const encoded = encodeURIComponent(operationId);
  const cases = [
    [getApplicationUpdateOperation, `/api/timeline/application-update-operations/${encoded}`],
    [
      getReviewTimelineEntryEditOperation,
      `/api/reviews/timeline-entry-edit-operations/${encoded}`,
    ],
    [getReviewRecordOperation, `/api/reviews/record-operations/${encoded}`],
  ];

  for (const [index, [request, expectedUrl]] of cases.entries()) {
    const result = await request(operationId, { signal: external.signal });
    assert.equal(result.operation_id, ["application", "timeline-entry-edit", "record"][index]);
    const [url, init] = fetchArguments(fetchMock, index);
    assert.equal(url, expectedUrl);
    assertInternalRead(init, external.signal);
  }
});

test("undo command status reads remain bound to their encoded command resource", async (context) => {
  const fetchMock = context.mock.method(
    globalThis,
    "fetch",
    async () => jsonResponse({ state: "absent", terminal: false }),
  );
  const external = new AbortController();
  const commandId = "command/id 中文";
  const encoded = encodeURIComponent(commandId);
  const cases = [
    [getApplicationUpdateUndoCommandStatus, `/api/timeline/application-update-undo-commands/${encoded}`],
    [
      getReviewTimelineEntryEditUndoCommandStatus,
      `/api/reviews/timeline-entry-edit-undo-commands/${encoded}`,
    ],
  ];

  for (const [index, [request, expectedUrl]] of cases.entries()) {
    await request(commandId, { signal: external.signal });
    const [url, init] = fetchArguments(fetchMock, index);
    assert.equal(url, expectedUrl);
    assertInternalRead(init, external.signal);
  }
});

test("trusted undo and prepare commands keep exact JSON bodies", async (context) => {
  const fetchMock = context.mock.method(
    globalThis,
    "fetch",
    async () => jsonResponse({ operation_id: "result" }),
  );
  const external = new AbortController();
  const operationId = "operation/id 中文";
  const commandId = "command/id 中文";
  const encoded = encodeURIComponent(operationId);
  const cases = [
    [
      () => undoApplicationUpdateOperation(operationId, commandId, { signal: external.signal }),
      `/api/timeline/application-update-operations/${encoded}/undo`,
      { command_id: commandId },
    ],
    [
      () => undoReviewTimelineEntryEditOperation(
        operationId,
        commandId,
        { signal: external.signal },
      ),
      `/api/reviews/timeline-entry-edit-operations/${encoded}/undo`,
      { command_id: commandId },
    ],
    [
      () => prepareReviewRecordUndoOperation(operationId, { signal: external.signal }),
      `/api/reviews/record-operations/${encoded}/prepare-undo`,
      {},
    ],
  ];

  for (const [index, [request, expectedUrl, body]] of cases.entries()) {
    await request();
    const [url, init] = fetchArguments(fetchMock, index);
    assert.equal(url, expectedUrl);
    assert.ok(init.signal instanceof AbortSignal);
    assert.notStrictEqual(init.signal, external.signal);
    assert.deepEqual(init, {
      method: "POST",
      headers: WRITE_JSON_HEADERS,
      body: JSON.stringify(body),
      signal: init.signal,
    });
  }
});

test("pending Review reads preserve their distinct envelopes and signal policy", async (context) => {
  const operations = [{ operation_id: "pending" }];
  const fetchMock = context.mock.method(
    globalThis,
    "fetch",
    async () => jsonResponse({ operations }),
  );
  const external = new AbortController();

  assert.deepEqual(
    await getPendingReviewRecordClarifications({ signal: external.signal }),
    operations,
  );
  assert.deepEqual(
    await getPendingReviewRecordConfirmations({ signal: external.signal }),
    operations,
  );
  assert.deepEqual(await getPendingReviewUndoOperations(), operations);

  const [recordUrl, recordInit] = fetchArguments(fetchMock, 0);
  assert.equal(recordUrl, "/api/reviews/record-operations/pending-clarifications");
  assertInternalRead(recordInit, external.signal);
  const [confirmationUrl, confirmationInit] = fetchArguments(fetchMock, 1);
  assert.equal(confirmationUrl, "/api/reviews/record-operations/pending-confirmations");
  assertInternalRead(confirmationInit, external.signal);
  const [undoUrl, undoInit] = fetchArguments(fetchMock, 2);
  assert.equal(undoUrl, "/api/reviews/undo-operations/pending");
  assertInternalRead(undoInit);
});

test("Timeline Review entry delete prepares one exact server-frozen Undo proposal", async (context) => {
  const fetchMock = context.mock.method(
    globalThis,
    "fetch",
    async () => jsonResponse({ operation_id: "undo" }),
  );
  const fingerprint = "a".repeat(64);

  const response = await prepareTimelineReviewUndoOperation(12, 34, fingerprint);
  assert.equal(response.operation_id, "undo");
  const [url, init] = fetchArguments(fetchMock);
  assert.equal(
    url,
    "/api/reviews/timeline-applications/12/timeline-entries/34/prepare-undo",
  );
  assert.ok(init.signal instanceof AbortSignal);
  assert.deepEqual(init, {
    method: "POST",
    headers: WRITE_JSON_HEADERS,
    body: JSON.stringify({ expected_fingerprint: fingerprint }),
    signal: init.signal,
  });
});

test("Review confirmation approve and reject preserve one encoded resource", async (context) => {
  const fetchMock = context.mock.method(
    globalThis,
    "fetch",
    async () => jsonResponse({ operation_id: "record" }),
  );
  const operationId = "record/id 中文";
  const external = new AbortController();
  const base = `/api/reviews/record-operations/${encodeURIComponent(operationId)}`;

  await approveReviewRecordOperation(operationId, { signal: external.signal });
  await rejectReviewRecordOperation(operationId, { signal: external.signal });

  for (const [index, suffix] of [[0, "/approve"], [1, "/reject"]]) {
    const [url, init] = fetchArguments(fetchMock, index);
    assert.equal(url, `${base}${suffix}`);
    assert.ok(init.signal instanceof AbortSignal);
    assert.notStrictEqual(init.signal, external.signal);
    assert.deepEqual(init, {
      method: "POST",
      headers: WRITE_JSON_HEADERS,
      body: "{}",
      signal: init.signal,
    });
  }
});

test("one Review batch decision submits every selected and excluded operation once", async (context) => {
  const operations = [{ operation_id: "first" }, { operation_id: "second" }];
  const fetchMock = context.mock.method(
    globalThis,
    "fetch",
    async () => jsonResponse({ operations }),
  );
  const clientTurnId = "turn/id 中文";
  const decisions = [
    { operation_id: "first", action: "approve" },
    { operation_id: "second", action: "reject" },
  ];
  const external = new AbortController();

  assert.deepEqual(await decideReviewRecordOperationsByClientTurn(
    clientTurnId,
    decisions,
    { signal: external.signal },
  ), operations);
  const [url, init] = fetchArguments(fetchMock);
  assert.equal(
    url,
    `/api/reviews/record-operations/by-client-turn/${encodeURIComponent(clientTurnId)}/decide`,
  );
  assert.ok(init.signal instanceof AbortSignal);
  assert.notStrictEqual(init.signal, external.signal);
  assert.deepEqual(init, {
    method: "POST",
    headers: WRITE_JSON_HEADERS,
    body: JSON.stringify({ decisions }),
    signal: init.signal,
  });
});

test("Review Undo detail, approve, and reject preserve one encoded resource", async (context) => {
  const fetchMock = context.mock.method(
    globalThis,
    "fetch",
    async () => jsonResponse({ operation_id: "undo" }),
  );
  const operationId = "undo/id 中文";
  const base = `/api/reviews/undo-operations/${encodeURIComponent(operationId)}`;

  await getReviewUndoOperation(operationId);
  await approveReviewUndoOperation(operationId);
  await rejectReviewUndoOperation(operationId);

  const [readUrl, readInit] = fetchArguments(fetchMock, 0);
  assert.equal(readUrl, base);
  assertInternalRead(readInit);
  for (const [index, suffix] of [[1, "/approve"], [2, "/reject"]]) {
    const [url, init] = fetchArguments(fetchMock, index);
    assert.equal(url, `${base}${suffix}`);
    assert.ok(init.signal instanceof AbortSignal);
    assert.deepEqual(init, {
      method: "POST",
      headers: WRITE_JSON_HEADERS,
      body: "{}",
      signal: init.signal,
    });
  }
});
