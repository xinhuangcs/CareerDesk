import assert from "node:assert/strict";
import { test } from "node:test";

import {
  defaultExpandedSectionIds,
  formatAdaptationDateTime,
  formatAdaptationElapsed,
  resumeAdaptationStateView,
} from "./resumeAdaptationState.ts";

function response(state, overrides = {}) {
  return {
    state,
    message: "message must never select a branch",
    cached: false,
    bound_resume: null,
    resume_options: [],
    recommended_resume_id: null,
    research: null,
    report: null,
    envelope: null,
    host_limitations: [],
    analysis_flags: [],
    estimated_input_tokens: null,
    model_disclosure: null,
    summarization_available: false,
    no_research_fallback_available: false,
    model_input_preview_available: false,
    ...overrides,
  };
}

test("the complete server state matrix maps to one explicit UI action", () => {
  const cases = [
    ["ready", "generate", true],
    ["generation_running", null, false],
    ["ok", null, false],
    ["no_resume", "upload_resume", false],
    ["resume_selection_required", "choose_resume", false],
    ["resume_reupload_required", "reupload_resume", false],
    ["missing_jd", "edit_jd", false],
    ["research_required", "run_research", false],
    ["research_running", null, false],
    ["research_failed", "run_research", false],
    ["research_disabled", "configure_research", false],
    ["research_unavailable", "configure_research", false],
    ["model_required", "configure_model", false],
    ["insufficient_model_capacity", "configure_model", false],
    ["invalid_model_output", "retry", true],
    ["stale", "reload", false],
    ["provider_error", "retry", true],
  ];
  for (const [state, action, visible] of cases) {
    const view = resumeAdaptationStateView(response(state));
    assert.equal(view.primary_action, action, state);
    assert.equal(view.adaptation_button_visible, visible, state);
  }
});

test("only the two narrow server flags expose informed fallback actions", () => {
  for (const state of ["research_disabled", "research_unavailable"]) {
    const view = resumeAdaptationStateView(response(state, {
      no_research_fallback_available: true,
    }));
    assert.equal(view.primary_action, "continue_without_research");
    assert.equal(view.adaptation_button_visible, true);
  }
  assert.equal(resumeAdaptationStateView(response("research_failed", {
    no_research_fallback_available: true,
  })).adaptation_button_visible, false);

  const summarized = resumeAdaptationStateView(response("insufficient_model_capacity", {
    summarization_available: true,
  }));
  assert.equal(summarized.primary_action, "confirm_summarized");
  assert.equal(summarized.adaptation_button_visible, true);
});

function section(section_id, assessment) {
  return {
    section_id,
    title: section_id,
    assessment,
    conclusion: "",
    rationale: "",
    preparation_points: [],
    improvements: [],
    rewrites: [],
  };
}

test("default-open selection uses priority while preserving the report array order", () => {
  const sections = [
    section("keep-first", "keep"),
    section("aligned-first", "aligned"),
    section("work-first", "needs_work"),
    section("high-first", "highly_aligned"),
    section("admin", "administrative"),
    section("high-second", "highly_aligned"),
    section("work-second", "needs_work"),
    section("aligned-second", "aligned"),
  ];
  const originalOrder = sections.map((item) => item.section_id);
  const open = defaultExpandedSectionIds(sections, 5);

  assert.deepEqual([...open], ["high-first", "high-second", "work-first", "work-second", "aligned-first"]);
  assert.deepEqual(sections.map((item) => item.section_id), originalOrder);
  assert.equal(open.has("keep-first"), false);
  assert.equal(open.has("admin"), false);
});

test("elapsed waiting copy has stable second and minute forms", () => {
  assert.equal(formatAdaptationElapsed(0), "0秒");
  assert.equal(formatAdaptationElapsed(59.9), "59秒");
  assert.equal(formatAdaptationElapsed(61), "1分01秒");
  assert.equal(formatAdaptationElapsed(Number.NaN), "0秒");
  assert.equal(formatAdaptationElapsed(61, "en"), "1m 01s");
});

test("adaptation timestamps use local year-month-day minute precision", () => {
  const formatted = formatAdaptationDateTime("2026-07-21T10:40:01.772741+00:00");
  assert.match(formatted, /^2026年\d{1,2}月\d{1,2}日 \d{2}:\d{2}$/);
  assert.doesNotMatch(formatted, /T|:\d{2}:\d{2}|\.772741|\+00:00/);
  assert.equal(formatAdaptationDateTime("not-a-time"), "not-a-time");
  assert.match(formatAdaptationDateTime("2026-07-21T10:40:01.772741+00:00", "en"), /2026/);
});
