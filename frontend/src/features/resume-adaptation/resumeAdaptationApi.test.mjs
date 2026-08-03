import assert from "node:assert/strict";
import { test } from "node:test";

import {
  bindApplicationResume,
  generateResumeAdaptation,
  getResumeAdaptation,
  getResumeAdaptationInputPreview,
} from "./resumeAdaptationApi.ts";

function jsonResponse(payload) {
  return new Response(JSON.stringify(payload), {
    status: 200,
    headers: { "Content-Type": "application/json" },
  });
}

function resume(id = 7, name = "岗位版") {
  return {
    id,
    name,
    updated_time: null,
    extraction_receipt: null,
  };
}

function readyResponse() {
  const selected = resume();
  return {
    state: "ready",
    message: null,
    cached: false,
    bound_resume: selected,
    resume_options: [selected],
    recommended_resume_id: null,
    research: null,
    report: null,
    envelope: null,
    host_limitations: [],
    analysis_flags: [],
    estimated_input_tokens: 123,
    model_disclosure: null,
    summarization_available: false,
    no_research_fallback_available: false,
    model_input_preview_available: true,
  };
}

function fetchArguments(fetchMock, index = 0) {
  assert.ok(fetchMock.mock.calls[index], `missing fetch call ${index}`);
  return fetchMock.mock.calls[index].arguments;
}

test("adaptation GET and input preview are bodyless no-store reads", async (context) => {
  const responses = [
    readyResponse(),
    {
      resume_id: 7,
      resume_name: "岗位版",
      input_form: "full_text",
      text: "完整抽取文本",
      host_limitations: [],
    },
  ];
  const fetchMock = context.mock.method(globalThis, "fetch", async () => jsonResponse(responses.shift()));
  const controller = new AbortController();

  assert.equal((await getResumeAdaptation(23, { signal: controller.signal })).state, "ready");
  assert.equal((await getResumeAdaptationInputPreview(23, { signal: controller.signal })).text, "完整抽取文本");
  assert.deepEqual(fetchArguments(fetchMock, 0), [
    "/api/timeline/applications/23/resume-adaptation?locale=en",
    { cache: "no-store", signal: controller.signal },
  ]);
  assert.deepEqual(fetchArguments(fetchMock, 1), [
    "/api/timeline/applications/23/resume-adaptation/input-preview?locale=en",
    { cache: "no-store", signal: controller.signal },
  ]);
});

test("adaptation POST always freezes output locale with the generation inputs", async (context) => {
  const fetchMock = context.mock.method(
    globalThis,
    "fetch",
    async () => jsonResponse(readyResponse()),
  );
  const controller = new AbortController();

  await generateResumeAdaptation(31, {
    refresh: true,
    expected_resume_id: 7,
    accept_no_research: true,
    accept_summarized: false,
  }, { signal: controller.signal });
  await generateResumeAdaptation(31, { refresh: false });

  const headers = { "X-CareerDesk-Request": "1", "Content-Type": "application/json" };
  assert.deepEqual(fetchArguments(fetchMock, 0), [
    "/api/timeline/applications/31/resume-adaptation",
    {
      method: "POST",
      headers,
      body: JSON.stringify({
        refresh: true,
        expected_resume_id: 7,
        accept_no_research: true,
        accept_summarized: false,
        output_locale: "en",
      }),
      signal: controller.signal,
    },
  ]);
  assert.deepEqual(fetchArguments(fetchMock, 1), [
    "/api/timeline/applications/31/resume-adaptation",
    {
      method: "POST",
      headers,
      body: JSON.stringify({
        refresh: false,
        expected_resume_id: null,
        accept_no_research: false,
        accept_summarized: false,
        output_locale: "en",
      }),
      signal: undefined,
    },
  ]);
});

test("resume binding PUT carries only the selected resume and edit CAS", async (context) => {
  const selected = resume(9, "英文岗位版");
  const fetchMock = context.mock.method(globalThis, "fetch", async () => jsonResponse({
    resume_id: 9,
    edit_revision: 12,
    bound_resume: selected,
  }));

  const response = await bindApplicationResume(41, {
    resume_id: 9,
    expected_edit_revision: 11,
  });
  assert.equal(response.edit_revision, 12);
  assert.deepEqual(fetchArguments(fetchMock), [
    "/api/timeline/applications/41/resume-binding",
    {
      method: "PUT",
      headers: { "X-CareerDesk-Request": "1", "Content-Type": "application/json" },
      body: JSON.stringify({ resume_id: 9, expected_edit_revision: 11 }),
    },
  ]);
});

test("invalid application ids fail before any HTTP request", async (context) => {
  const fetchMock = context.mock.method(globalThis, "fetch", async () => jsonResponse({}));
  await assert.rejects(() => getResumeAdaptation(0), TypeError);
  await assert.rejects(() => generateResumeAdaptation(-1, { refresh: false }), TypeError);
  assert.equal(fetchMock.mock.callCount(), 0);
});
