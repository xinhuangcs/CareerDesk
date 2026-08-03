import assert from "node:assert/strict";
import { test } from "node:test";

import {
  answerGrill,
  claimGrillExperimentIntro,
  deleteGrillSession,
  finalizeGrillSession,
  getGrillSessionSummary,
  getGrillSessions,
  resumeGrill,
  skipGrill,
  startGrill,
  suspendGrill,
} from "./grillApi.ts";

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

const WRITE_JSON_HEADERS = {
  "X-CareerDesk-Request": "1",
  "Content-Type": "application/json",
};

test("the Grill introduction claim lets the backend identify the installed release", async (context) => {
  const fetchMock = context.mock.method(
    globalThis,
    "fetch",
    async () => jsonResponse({ should_show: true, release_version: "1.1.0" }),
  );

  assert.deepEqual(await claimGrillExperimentIntro(), {
    should_show: true,
    release_version: "1.1.0",
  });
  assert.deepEqual(fetchArguments(fetchMock), [
    "/api/grill/experiment-intro/claim",
    {
      method: "POST",
      headers: WRITE_JSON_HEADERS,
      body: "{}",
      signal: undefined,
    },
  ]);
});

test("Grill session lists preserve both filters and project the items envelope", async (context) => {
  const session = (id, state) => ({ id, question_set_id: 9, kind: "generated", edition: "basic", context_label: "简历", state, answered: 0, total: 5, started_time: "2026-07-20T00:00:00+00:00", ended_time: state === "finished" ? "2026-07-20T00:01:00+00:00" : null });
  const active = [session(1, "active")];
  const finished = [session(2, "finished")];
  const responses = [{ items: active }, { items: finished }];
  const fetchMock = context.mock.method(
    globalThis,
    "fetch",
    async () => jsonResponse(responses.shift()),
  );

  assert.deepEqual(await getGrillSessions("active,suspended"), active);
  assert.deepEqual(await getGrillSessions("finished"), finished);
  assert.deepEqual(fetchArguments(fetchMock, 0), [
    "/api/grill/sessions?state=active,suspended",
    undefined,
  ]);
  assert.deepEqual(fetchArguments(fetchMock, 1), [
    "/api/grill/sessions?state=finished",
    undefined,
  ]);
});

test("Grill start and resume keep their exact trusted JSON bodies", async (context) => {
  const responses = [{ status: "ok", session_id: 7 }, { status: "ok", session_id: 7 }];
  const fetchMock = context.mock.method(
    globalThis,
    "fetch",
    async () => jsonResponse(responses.shift()),
  );
  await startGrill(31, 7);
  await resumeGrill(7);

  assert.deepEqual(fetchArguments(fetchMock, 0), [
    "/api/grill/start",
    {
      method: "POST",
      headers: WRITE_JSON_HEADERS,
      body: JSON.stringify({ question_set_id: 31, question_count: 7 }),
      signal: undefined,
    },
  ]);
  assert.deepEqual(fetchArguments(fetchMock, 1), [
    "/api/grill/resume",
    {
      method: "POST",
      headers: WRITE_JSON_HEADERS,
      body: JSON.stringify({ session_id: 7 }),
      signal: undefined,
    },
  ]);
});

test("Grill answer, skip, and suspend keep their exact command shapes", async (context) => {
  const fetchMock = context.mock.method(
    globalThis,
    "fetch",
    async () => jsonResponse({ status: "ok" }),
  );

  await answerGrill(11, 13, "我的回答", false);
  await skipGrill(11, 17);
  await suspendGrill(11);
  await answerGrill(11, 13, "追问答案", true);

  const cases = [
    ["/api/grill/answer", { session_id: 11, session_item_id: 13, text: "我的回答", answering_follow_up: false }],
    ["/api/grill/skip", { session_id: 11, session_item_id: 17 }],
    ["/api/grill/suspend", { session_id: 11 }],
    ["/api/grill/answer", { session_id: 11, session_item_id: 13, text: "追问答案", answering_follow_up: true }],
  ];
  for (const [index, [url, body]] of cases.entries()) {
    assert.deepEqual(fetchArguments(fetchMock, index), [
      url,
      {
        method: "POST",
        headers: WRITE_JSON_HEADERS,
        body: JSON.stringify(body),
        signal: undefined,
      },
    ]);
  }
});

test("Grill finalize and delete preserve the numeric session resource", async (context) => {
  let call = 0;
  const fetchMock = context.mock.method(
    globalThis,
    "fetch",
    async () => jsonResponse(call++ === 0
      ? { status: "ok", answers: [], summary: null }
      : { status: "ok" }),
  );

  await finalizeGrillSession(23);
  await deleteGrillSession(23);

  assert.deepEqual(fetchArguments(fetchMock, 0), [
    "/api/grill/sessions/23/finalize",
    {
      method: "POST",
      headers: WRITE_JSON_HEADERS,
      body: "{}",
      signal: undefined,
    },
  ]);
  assert.deepEqual(fetchArguments(fetchMock, 1), [
    "/api/grill/sessions/23",
    {
      method: "DELETE",
      headers: { "X-CareerDesk-Request": "1" },
    },
  ]);
});

test("Grill finished-session deep links use the read-only summary endpoint", async (context) => {
  const fetchMock = context.mock.method(
    globalThis,
    "fetch",
    async () => jsonResponse({ status: "ok", session_id: 23, answers: [], summary: {} }),
  );

  assert.equal((await getGrillSessionSummary(23)).session_id, 23);
  assert.deepEqual(fetchArguments(fetchMock), [
    "/api/grill/sessions/23/summary",
    undefined,
  ]);
});
