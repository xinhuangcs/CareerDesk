import assert from "node:assert/strict";
import { test } from "node:test";

import { WRITE_HEADERS } from "./headers.ts";
import {
  HttpError,
  del,
  getJson,
  postForm,
  postJson,
  putForm,
  putJson,
} from "./transport.ts";

function jsonResponse(payload, status = 200, headers = {}) {
  return new Response(JSON.stringify(payload), {
    status,
    headers: { "Content-Type": "application/json", ...headers },
  });
}

function fetchArguments(fetchMock, index = 0) {
  assert.ok(fetchMock.mock.calls[index], `missing fetch call ${index}`);
  return fetchMock.mock.calls[index].arguments;
}

test("the same frozen write marker is the single header source", () => {
  assert.deepEqual(WRITE_HEADERS, { "X-CareerDesk-Request": "1" });
  assert.equal(Object.isFrozen(WRITE_HEADERS), true);

  const error = new HttpError(409, "conflict");
  assert.ok(error instanceof Error);
  assert.ok(error instanceof HttpError);
  assert.equal(error.name, "HttpError");
  assert.equal(error.status, 409);
  assert.equal(error.message, "conflict");
  assert.equal(error.requestId, undefined);
  assert.equal(error.code, undefined);
  assert.equal(error.problemType, undefined);
  assert.equal(error.errors, undefined);
  assert.equal(error.params, undefined);
});

test("getJson forwards the exact RequestInit and returns parsed JSON", async (context) => {
  const response = { status: "ok", nested: { count: 2 } };
  const fetchMock = context.mock.method(
    globalThis,
    "fetch",
    async () => jsonResponse(response),
  );
  const controller = new AbortController();
  const init = {
    cache: "no-store",
    headers: { Accept: "application/json" },
    signal: controller.signal,
  };

  assert.deepEqual(await getJson("/example?exact=1", init), response);
  assert.equal(fetchMock.mock.callCount(), 1);
  const [url, actualInit] = fetchArguments(fetchMock);
  assert.equal(url, "/example?exact=1");
  assert.strictEqual(actualInit, init);
});

test("postJson preserves POST headers, JSON encoding, signal, and default body", async (context) => {
  const responses = [
    { request: "explicit" },
    { request: "default" },
  ];
  const fetchMock = context.mock.method(
    globalThis,
    "fetch",
    async () => jsonResponse(responses.shift()),
  );
  const controller = new AbortController();
  const body = { text: "保留原样", nested: { enabled: true } };

  assert.deepEqual(
    await postJson("/write", body, { signal: controller.signal }),
    { request: "explicit" },
  );
  assert.deepEqual(await postJson("/write-default"), { request: "default" });

  assert.equal(fetchMock.mock.callCount(), 2);
  const [firstUrl, firstInit] = fetchArguments(fetchMock, 0);
  assert.equal(firstUrl, "/write");
  assert.equal(firstInit.method, "POST");
  assert.deepEqual(firstInit.headers, {
    "X-CareerDesk-Request": "1",
    "Content-Type": "application/json",
  });
  assert.equal(firstInit.body, JSON.stringify(body));
  assert.strictEqual(firstInit.signal, controller.signal);

  const [secondUrl, secondInit] = fetchArguments(fetchMock, 1);
  assert.equal(secondUrl, "/write-default");
  assert.equal(secondInit.method, "POST");
  assert.equal(secondInit.body, "{}");
  assert.equal(Object.hasOwn(secondInit, "signal"), true);
  assert.equal(secondInit.signal, undefined);
});

test("form requests preserve the browser-owned multipart boundary", async (context) => {
  const responses = [
    { upload: "created" },
    { upload: "replaced" },
  ];
  const fetchMock = context.mock.method(
    globalThis,
    "fetch",
    async () => jsonResponse(responses.shift()),
  );
  const controller = new AbortController();
  const postBody = new FormData();
  postBody.append("field", "post");
  const putBody = new FormData();
  putBody.append("field", "put");

  assert.deepEqual(
    await postForm("/upload", postBody, { signal: controller.signal }),
    { upload: "created" },
  );
  assert.deepEqual(await putForm("/replace", putBody), { upload: "replaced" });

  const [postUrl, postInit] = fetchArguments(fetchMock, 0);
  assert.equal(postUrl, "/upload");
  assert.equal(postInit.method, "POST");
  assert.strictEqual(postInit.headers, WRITE_HEADERS);
  assert.strictEqual(postInit.body, postBody);
  assert.strictEqual(postInit.signal, controller.signal);
  assert.equal("Content-Type" in postInit.headers, false);

  const [putUrl, putInit] = fetchArguments(fetchMock, 1);
  assert.equal(putUrl, "/replace");
  assert.equal(putInit.method, "PUT");
  assert.strictEqual(putInit.headers, WRITE_HEADERS);
  assert.strictEqual(putInit.body, putBody);
  assert.equal("Content-Type" in putInit.headers, false);
  assert.equal(Object.hasOwn(putInit, "signal"), false);
});

test("delete and JSON PUT keep their exact wire shapes", async (context) => {
  const responses = [
    { deletion: "complete" },
    { update: "complete" },
  ];
  const fetchMock = context.mock.method(
    globalThis,
    "fetch",
    async () => jsonResponse(responses.shift()),
  );
  const body = { flag: "good" };

  assert.deepEqual(await del("/resource/7"), { deletion: "complete" });
  assert.deepEqual(await putJson("/resource/7", body), { update: "complete" });

  const [deleteUrl, deleteInit] = fetchArguments(fetchMock, 0);
  assert.equal(deleteUrl, "/resource/7");
  assert.deepEqual(deleteInit, { method: "DELETE", headers: WRITE_HEADERS });
  assert.strictEqual(deleteInit.headers, WRITE_HEADERS);

  const [putUrl, putInit] = fetchArguments(fetchMock, 1);
  assert.equal(putUrl, "/resource/7");
  assert.deepEqual(putInit, {
    method: "PUT",
    headers: {
      "X-CareerDesk-Request": "1",
      "Content-Type": "application/json",
    },
    body: JSON.stringify(body),
  });
  assert.equal(Object.hasOwn(putInit, "signal"), false);
});

test("HTTP errors preserve stable metadata without exposing backend diagnostic copy", async (context) => {
  const responses = [
    jsonResponse({
      type: "urn:careerdesk:problem:conflict",
      status: 409,
      detail: "  exact backend detail  ",
      code: "state_conflict",
      params: { revision: 7 },
      request_id: "body-request-id",
    }, 409, { "X-Request-ID": "header-request-id" }),
    jsonResponse({
      detail: "请求参数校验失败。",
      errors: [
        { msg: "field required" },
        { ignored: true },
        { msg: 123 },
        null,
      ],
      request_id: "validation-request-id",
    }, 422),
    new Response("not-json", { status: 502, headers: { "X-Request-ID": "proxy-id" } }),
  ];
  const fetchMock = context.mock.method(
    globalThis,
    "fetch",
    async () => responses.shift(),
  );
  const expected = [
    [409, "This item changed elsewhere. Refresh and try again."],
    [422, "Some fields are missing or invalid. Check the form and try again."],
    [502, "The service is temporarily unavailable. Try again later."],
  ];

  const captured = [];
  for (const [status, message] of expected) {
    await assert.rejects(
      getJson("/failure"),
      (reason) => {
        captured.push(reason);
        return reason instanceof HttpError
          && reason.name === "HttpError"
          && reason.status === status
          && reason.message === message;
      },
    );
  }
  assert.equal(fetchMock.mock.callCount(), expected.length);
  assert.equal(captured[0].requestId, "header-request-id");
  assert.equal(captured[0].code, "state_conflict");
  assert.equal(captured[0].problemType, "urn:careerdesk:problem:conflict");
  assert.deepEqual(captured[0].params, { revision: 7 });
  assert.deepEqual(captured[1].errors, [
    { msg: "field required" },
    { ignored: true },
    { msg: 123 },
    null,
  ]);
  assert.equal(captured[1].requestId, "validation-request-id");
  assert.equal(captured[2].requestId, "proxy-id");
});

test("all six request helpers reject non-2xx responses through the same HttpError", async (context) => {
  const fetchMock = context.mock.method(
    globalThis,
    "fetch",
    async () => jsonResponse({ detail: "shared rejection" }, 418),
  );
  const form = new FormData();
  form.append("field", "value");
  const requests = [
    ["getJson", () => getJson("/error/get")],
    ["postJson", () => postJson("/error/post", { value: 1 })],
    ["postForm", () => postForm("/error/post-form", form)],
    ["del", () => del("/error/delete")],
    ["putJson", () => putJson("/error/put", { value: 1 })],
    ["putForm", () => putForm("/error/put-form", form)],
  ];

  for (const [name, request] of requests) {
    await assert.rejects(
      request(),
      (reason) => reason instanceof HttpError
        && reason.constructor === HttpError
        && reason.name === "HttpError"
        && reason.status === 418
        && reason.message === "Request did not complete (HTTP 418).",
      `${name} must call ensureOk before parsing success JSON`,
    );
  }
  assert.equal(fetchMock.mock.callCount(), requests.length);
});

test("network failures are not rewritten as HTTP errors", async (context) => {
  const networkFailure = new TypeError("network unavailable");
  context.mock.method(globalThis, "fetch", async () => {
    throw networkFailure;
  });

  await assert.rejects(
    getJson("/network-failure"),
    (reason) => reason === networkFailure,
  );
});
