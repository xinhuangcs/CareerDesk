import assert from "node:assert/strict";
import { test } from "node:test";

import {
  RESUME_ADAPTATION_STATES,
  parseResumeAdaptationInputPreview,
  parseResumeAdaptationResponse,
  parseResumeBindingResponse,
} from "./resumeAdaptationContract.ts";

function resume(id = 7, name = "岗位版") {
  return {
    id,
    name,
    updated_time: "2026-07-19T12:00:00Z",
    extraction_receipt: {
      status: "usable",
      char_count: 100,
      non_whitespace_count: 90,
      alnum_count: 80,
      replacement_char_count: 0,
      replacement_ratio: 0,
      control_char_count: 0,
      control_ratio: 0,
      reason_codes: [],
      warning_codes: [],
    },
  };
}

function evidence(segment_id, text, char_start = 0) {
  return {
    segment_id,
    char_start,
    char_end: char_start + text.length,
    text,
  };
}

function research(overrides = {}) {
  return {
    artifact_state: "ready",
    attempt_state: "succeeded",
    coverage_quality: "complete",
    fresh_until: "2026-08-01T00:00:00Z",
    error_code: null,
    action: null,
    ...overrides,
  };
}

function fullReport() {
  return {
    mode: "full",
    fit_band: "strong",
    summary_sentences: ["整体证据充分。"],
    requirement_assessments: [{
      requirement_summary: "负责平台架构",
      requirement_kind: "must",
      evidence_state: "strong",
      jd_segment_refs: ["J1-0001-a1b2c3d4"],
      resume_segment_refs: ["R1-0002-b1c2d3e4"],
      limitation: "只依据简历已写事实",
      jd_evidence: [evidence("J1-0001-a1b2c3d4", "负责平台架构")],
      resume_evidence: [evidence("R1-0002-b1c2d3e4", "主导平台改造", 10)],
    }],
    overall_advice: [{ action: "前置平台成果", reason: "对应首要职责" }],
    section_reviews: [{
      section_id: "S1-0001-f1a2b3c4",
      section_name: "工作经历",
      resume_segment_start_ref: "R1-0001-c1d2e3f4",
      resume_segment_end_ref: "R1-0003-d1e2f3a4",
      resume_segment_range: {
        start: evidence("R1-0001-c1d2e3f4", "工作经历"),
        end: evidence("R1-0003-d1e2f3a4", "交付关键能力", 18),
      },
      assessment: "highly_aligned",
      conclusion: "核心经历契合",
      reasoning: "有可核验的职责与结果",
      preparation_points: ["准备架构权衡"],
      improvements: ["把结果提前"],
      rewrites: [{
        resume_segment_ref: "R1-0002-b1c2d3e4",
        original_text: "负责平台改造",
        suggestion: "主导平台改造并交付关键能力",
        reason: "突出 ownership",
        verification_needed: true,
      }],
    }],
    major_gaps: [],
    next_steps: [],
    analysis_caveats: ["未分析视觉排版"],
  };
}

function envelope() {
  return {
    artifact_version: 1,
    resume_id: 7,
    resume_name: "岗位版",
    resume_selection: "bound",
    research_mode: "snapshot",
    research_snapshot_id: "snapshot-1",
    resume_input_form: "full_text",
    generated_time: "2026-07-19T12:30:00Z",
    content_locale: "zh-CN",
  };
}

function baseResponse(state = "ready") {
  return {
    state,
    message: "只作为补充展示，不能决定状态",
    cached: false,
    bound_resume: resume(),
    resume_options: [resume(), resume(8, "通用版")],
    recommended_resume_id: null,
    research: research(),
    report: null,
    envelope: null,
    host_limitations: ["仅分析抽取文本"],
    analysis_flags: [],
    estimated_input_tokens: 12_345,
    model_disclosure: {
      provider: "openai",
      model: "gpt-example",
      label: "用户选择的模型",
    },
    summarization_available: false,
    no_research_fallback_available: false,
    model_input_preview_available: true,
  };
}

test("all frozen state codes parse without consulting message text", () => {
  for (const state of RESUME_ADAPTATION_STATES) {
    const payload = baseResponse(state);
    payload.message = state === "ready" ? "research_failed provider_error" : "ok ready";
    if (state === "ok") {
      payload.report = fullReport();
      payload.envelope = envelope();
    }
    if (state === "resume_selection_required") {
      payload.bound_resume = null;
      payload.recommended_resume_id = 7;
    }
    if (state === "no_resume") {
      payload.bound_resume = null;
      payload.resume_options = [];
    }
    const parsed = parseResumeAdaptationResponse(payload);
    assert.equal(parsed.state, state);
  }
});

test("the report parser accepts only the materialized public shape and projects evidence text", () => {
  const payload = baseResponse("ok");
  payload.report = fullReport();
  payload.envelope = envelope();
  const parsed = parseResumeAdaptationResponse(payload);

  assert.equal(parsed.report.mode, "full");
  assert.deepEqual(parsed.report.requirement_assessments[0].jd_evidence, ["负责平台架构"]);
  assert.deepEqual(parsed.report.requirement_assessments[0].resume_evidence, ["主导平台改造"]);
  assert.equal(parsed.report.section_reviews[0].title, "工作经历");
  assert.equal(parsed.report.section_reviews[0].rationale, "有可核验的职责与结果");
  assert.equal(parsed.report.section_reviews[0].rewrites[0].suggested_text, "主导平台改造并交付关键能力");
  assert.equal(parsed.report.section_reviews[0].resume_segment_range.start.text, "工作经历");
  assert.deepEqual(parsed.model_disclosure, {
    provider: "openai",
    model: "gpt-example",
    label: "用户选择的模型",
  });
});

test("gap and full branches remain mutually exclusive", () => {
  const payload = baseResponse("ok");
  payload.envelope = envelope();
  payload.report = {
    mode: "gap_brief",
    fit_band: "weak",
    summary_sentences: ["当前简历与核心职能差距较大。"],
    requirement_assessments: [{
      requirement_summary: "五年审计经验",
      requirement_kind: "must",
      evidence_state: "absent",
      jd_segment_refs: ["J1-0003-e1f2a3b4"],
      resume_segment_refs: [],
      limitation: "当前简历未展示相关经历",
      jd_evidence: [evidence("J1-0003-e1f2a3b4", "五年审计经验")],
      resume_evidence: [],
    }],
    overall_advice: [],
    section_reviews: [],
    major_gaps: [{
      requirement_summary: "五年审计经验",
      evidence_state: "absent",
      jd_segment_refs: ["J1-0003-e1f2a3b4"],
      resume_segment_refs: [],
      basis: "当前简历未展示审计经历",
      jd_evidence: [evidence("J1-0003-e1f2a3b4", "五年审计经验")],
      resume_evidence: [],
    }],
    next_steps: ["更换更相关的简历版本"],
    analysis_caveats: [],
  };
  assert.equal(parseResumeAdaptationResponse(payload).report.mode, "gap_brief");

  payload.report.section_reviews = fullReport().section_reviews;
  assert.throws(() => parseResumeAdaptationResponse(payload), TypeError);
  payload.report = fullReport();
  payload.report.fit_band = "weak";
  assert.throws(() => parseResumeAdaptationResponse(payload), TypeError);
});

test("ok, selection, unknown state, and malformed evidence invariants fail closed", () => {
  assert.throws(() => parseResumeAdaptationResponse(baseResponse("ok")), TypeError);
  const emptySelection = baseResponse("resume_selection_required");
  emptySelection.bound_resume = null;
  emptySelection.resume_options = [];
  emptySelection.recommended_resume_id = null;
  assert.throws(() => parseResumeAdaptationResponse(emptySelection), TypeError);
  assert.throws(() => parseResumeAdaptationResponse(baseResponse("made_up")), TypeError);

  const malformed = baseResponse("ok");
  malformed.report = fullReport();
  malformed.envelope = envelope();
  malformed.report.requirement_assessments[0].jd_evidence = [{ text: 42 }];
  assert.throws(() => parseResumeAdaptationResponse(malformed), TypeError);

  const invalidFull = baseResponse("ok");
  invalidFull.report = fullReport();
  invalidFull.envelope = envelope();
  invalidFull.report.next_steps = ["full 模式不能携带 gap 字段"];
  assert.throws(() => parseResumeAdaptationResponse(invalidFull), TypeError);

  const duplicateOptions = baseResponse();
  duplicateOptions.resume_options = [resume(), resume()];
  assert.throws(() => parseResumeAdaptationResponse(duplicateOptions), TypeError);
});

test("unknown and missing fields fail closed at every public DTO boundary", () => {
  const valid = baseResponse("ok");
  valid.report = fullReport();
  valid.envelope = envelope();

  const mutations = [
    (value) => { value.internal = "must not escape"; },
    (value) => { delete value.cached; },
    (value) => { value.bound_resume.file_path = "/private/resume.docx"; },
    (value) => { delete value.bound_resume.updated_time; },
    (value) => { value.bound_resume.extraction_receipt.source = "docx"; },
    (value) => { delete value.research.error_code; },
    (value) => { value.model_disclosure.endpoint = "https://provider.invalid"; },
    (value) => { value.envelope.input_hash = "host-only"; },
    (value) => { value.report.input_hash = "host-only"; },
    (value) => { value.report.requirement_assessments[0].file_path = "/private/path"; },
    (value) => { value.report.requirement_assessments[0].jd_evidence[0].url = "secret"; },
    (value) => { delete value.report.section_reviews[0].resume_segment_range; },
    (value) => { value.report.section_reviews[0].resume_segment_range.content_text = "secret"; },
    (value) => { value.report.section_reviews[0].rewrites[0].verification_notes = []; },
  ];

  for (const mutate of mutations) {
    const malformed = structuredClone(valid);
    mutate(malformed);
    assert.throws(() => parseResumeAdaptationResponse(malformed), TypeError);
  }
});

test("provider aliases and string refs cannot stand in for the public materialized fields", () => {
  const alias = baseResponse("ok");
  alias.report = fullReport();
  alias.envelope = envelope();
  alias.report.requirement_assessments[0].requirement =
    alias.report.requirement_assessments[0].requirement_summary;
  delete alias.report.requirement_assessments[0].requirement_summary;
  assert.throws(() => parseResumeAdaptationResponse(alias), TypeError);

  const rewriteAlias = baseResponse("ok");
  rewriteAlias.report = fullReport();
  rewriteAlias.envelope = envelope();
  rewriteAlias.report.section_reviews[0].rewrites[0].suggested_text =
    rewriteAlias.report.section_reviews[0].rewrites[0].suggestion;
  delete rewriteAlias.report.section_reviews[0].rewrites[0].suggestion;
  assert.throws(() => parseResumeAdaptationResponse(rewriteAlias), TypeError);

  const stringEvidence = baseResponse("ok");
  stringEvidence.report = fullReport();
  stringEvidence.envelope = envelope();
  stringEvidence.report.requirement_assessments[0].jd_evidence = ["J1-0001-a1b2c3d4"];
  assert.throws(() => parseResumeAdaptationResponse(stringEvidence), TypeError);

  const missingMaterialization = baseResponse("ok");
  missingMaterialization.report = fullReport();
  missingMaterialization.envelope = envelope();
  delete missingMaterialization.report.requirement_assessments[0].resume_evidence;
  assert.throws(() => parseResumeAdaptationResponse(missingMaterialization), TypeError);

  const mismatchedEvidence = baseResponse("ok");
  mismatchedEvidence.report = fullReport();
  mismatchedEvidence.envelope = envelope();
  mismatchedEvidence.report.requirement_assessments[0].jd_evidence[0].segment_id =
    "J1-0099-a1b2c3d4";
  assert.throws(() => parseResumeAdaptationResponse(mismatchedEvidence), TypeError);

  const mismatchedRange = baseResponse("ok");
  mismatchedRange.report = fullReport();
  mismatchedRange.envelope = envelope();
  mismatchedRange.report.section_reviews[0].resume_segment_range.start.segment_id =
    "R1-0099-a1b2c3d4";
  assert.throws(() => parseResumeAdaptationResponse(mismatchedRange), TypeError);
});

test("non-ok states cannot smuggle a report or envelope", () => {
  for (const state of RESUME_ADAPTATION_STATES.filter((item) => item !== "ok")) {
    const withReport = baseResponse(state);
    withReport.report = fullReport();
    assert.throws(() => parseResumeAdaptationResponse(withReport), TypeError, state);

    const withEnvelope = baseResponse(state);
    withEnvelope.envelope = envelope();
    assert.throws(() => parseResumeAdaptationResponse(withEnvelope), TypeError, state);
  }
});

test("binding and model-input preview parsers preserve their exact public fields", () => {
  assert.deepEqual(parseResumeBindingResponse({
    resume_id: 7,
    edit_revision: 4,
    bound_resume: resume(),
  }), {
    resume_id: 7,
    edit_revision: 4,
    bound_resume: resume(),
  });
  assert.deepEqual(parseResumeAdaptationInputPreview({
    resume_id: 7,
    resume_name: "岗位版",
    input_form: "summarized",
    text: "压缩后的实际模型输入",
    host_limitations: ["不是全文"],
  }), {
    resume_id: 7,
    resume_name: "岗位版",
    input_form: "summarized",
    text: "压缩后的实际模型输入",
    host_limitations: ["不是全文"],
  });

  assert.throws(() => parseResumeBindingResponse({
    resume_id: 7,
    edit_revision: 4,
    bound_resume: resume(),
    internal: true,
  }), TypeError);
  assert.throws(() => parseResumeAdaptationInputPreview({
    resume_id: 7,
    resume_name: "岗位版",
    input_form: "summarized",
    text: "压缩后的实际模型输入",
  }), TypeError);
});
