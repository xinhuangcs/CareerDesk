import assert from "node:assert/strict";
import test from "node:test";

import { presentReplayReview } from "./grillReplayPresentation.ts";

test("replay presentation exposes readable feedback fields and guide text", () => {
  assert.deepEqual(presentReplayReview({
    strengths: ["结论清楚"],
    gaps: ["补充指标取舍"],
    next_step: "用一次具体实验说明。",
  }, {
    kind: "coaching_guide",
    text: "说明误报率、漏报率和多次运行稳定性。",
  }), {
    strengths: ["结论清楚"],
    gaps: ["补充指标取舍"],
    nextStep: "用一次具体实验说明。",
    guideText: "说明误报率、漏报率和多次运行稳定性。",
  });
});

test("a skipped answer hides empty feedback instead of exposing JSON objects", () => {
  assert.deepEqual(presentReplayReview({}, {
    kind: "coaching_guide",
    text: "  先比较评估目标，再说明指标。  ",
  }), {
    strengths: [],
    gaps: [],
    nextStep: null,
    guideText: "先比较评估目标，再说明指标。",
  });
});

test("malformed optional presentation fields fail closed", () => {
  assert.deepEqual(presentReplayReview({
    strengths: ["有效", 3, ""],
    gaps: "不是列表",
    next_step: {},
  }, {
    text: ["不是正文"],
  }), {
    strengths: ["有效"],
    gaps: [],
    nextStep: null,
    guideText: null,
  });
});
