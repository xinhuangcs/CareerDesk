import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

import {
  QUICK_PROMPT_GROUPS,
  QUICK_PROMPTS,
  QUICK_PROMPT_ROTATION,
} from "./chatQuickPrompts.ts";

const PROMPT_ICONS = new Set(["replay", "target", "clipboard", "bookmark", "board", "pulse"]);
const chatPageUrl = new URL("./ChatPage.tsx", import.meta.url);

test("homepage quick prompt configuration stays valid without freezing its catalogue", () => {
  assert.ok(QUICK_PROMPT_GROUPS.length > 0);
  assert.deepEqual(
    QUICK_PROMPTS,
    QUICK_PROMPT_GROUPS.flatMap((group) => group.prompts),
  );

  const labels = QUICK_PROMPT_GROUPS.map((group) => group.label);
  assert.equal(new Set(labels).size, labels.length);
  for (const group of QUICK_PROMPT_GROUPS) {
    assert.ok(group.label.trim());
    assert.ok(group.hint.trim());
    assert.ok(group.prompts.length > 0, `分组「${group.label}」不能是空的`);
  }

  const titles = QUICK_PROMPTS.map((prompt) => prompt.title);
  assert.equal(new Set(titles).size, titles.length);
  for (const prompt of QUICK_PROMPTS) {
    assert.ok(prompt.title.trim());
    assert.ok(prompt.hint.trim());
    assert.ok(prompt.text.trim());
    assert.ok(PROMPT_ICONS.has(prompt.icon));
  }
});

test("shortcuts that request application-prep generation preserve its safety boundary", () => {
  const generationPrompts = QUICK_PROMPTS.filter((prompt) => (
    /公司与岗位调研/.test(prompt.text) && /生成|启动|重试|刷新/.test(prompt.text)
  ));

  for (const prompt of generationPrompts) {
    assert.match(prompt.text, /缺失、失败或已过期/);
    assert.match(prompt.text, /不要刷新仍然有效的报告/);
  }
});

test("homepage rotation contains every prompt once and starts with mixed capabilities", () => {
  assert.equal(QUICK_PROMPT_ROTATION.length, QUICK_PROMPTS.length);
  assert.deepEqual(new Set(QUICK_PROMPT_ROTATION), new Set(QUICK_PROMPTS));

  const firstScreen = QUICK_PROMPT_ROTATION.slice(0, QUICK_PROMPT_GROUPS.length);
  for (const group of QUICK_PROMPT_GROUPS) {
    assert.ok(firstScreen.includes(group.prompts[0]));
  }
});

test("homepage renders a clean rotating example row without freezing its contents", async () => {
  const chatPage = await readFile(chatPageUrl, "utf8");

  assert.match(chatPage, /const QUICK_PROMPT_PAGE_SIZE = 4/);
  assert.match(chatPage, /aria-label=\{l\("换一组求职任务示例", "Show another set of job-search examples"\)\}/);
  assert.match(chatPage, /求职中遇到的任何问题/);
  assert.doesNotMatch(chatPage, /不限于以上/);
  assert.doesNotMatch(chatPage, /直接描述你想完成的事/);
  assert.doesNotMatch(chatPage, /我可以帮你整理岗位/);
  assert.doesNotMatch(chatPage, /还有其他需求？直接告诉我就好/);
  assert.doesNotMatch(chatPage, /md:grid-cols-3/);
});

test("each durable Chat request freezes the current output locale", async () => {
  const chatPage = await readFile(chatPageUrl, "utf8");

  assert.match(chatPage, /output_locale: currentOutputLocale\(\)/);
  assert.match(chatPage, /from "\.\.\/\.\.\/i18n\/localePreference"/);
});
