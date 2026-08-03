import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";

const pageSource = readFileSync(new URL("./QuestionsPage.tsx", import.meta.url), "utf8");

test("the catalogue exposes only the two generated practice types", () => {
  assert.match(pageSource, /\['basic', l\('通用练习', 'General practice'\)\]/);
  assert.match(pageSource, /\['custom', l\('岗位定制', 'Role-specific'\)\]/);
  assert.match(pageSource, /\/api\/questions\?edition=\$\{practiceType\}/);
  assert.doesNotMatch(pageSource, /library-snapshots|手动导入|网页导入|面试复盘/);
});

test("search language is concrete and the catalogue has no mutable answer generation", () => {
  assert.match(pageSource, /aria-label=\{l\("搜索题目关键词", "Search question keywords"\)\}/);
  assert.match(pageSource, /placeholder=\{l\("输入题目关键词", "Enter keywords"\)\}/);
  assert.doesNotMatch(pageSource, /搜索题目或能力|reference_answer|\/answer\/regenerate|\/improve/);
  assert.match(pageSource, /answer-guide-verification/);
  assert.match(pageSource, /competency-progress/);
});

test("answer guides render only their text field instead of serialized JSON", () => {
  assert.match(pageSource, /const guideText = answerGuideText\(item\.answer_guide\)/);
  assert.match(pageSource, />\{guideText\}<\/p>/);
  assert.doesNotMatch(pageSource, /JSON\.stringify\(item\.answer_guide/);
});
