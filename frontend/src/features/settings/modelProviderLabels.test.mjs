import assert from "node:assert/strict";
import { test } from "node:test";

import { modelProviderLabel } from "./modelProviderLabels.ts";

const bilingualProviders = [
  ["deepseek", "DeepSeek 深度求索", "DeepSeek"],
  ["dashscope", "阿里云百炼 通义千问", "Alibaba Cloud Model Studio Qwen"],
  ["moonshot", "月之暗面 Kimi", "Moonshot AI Kimi"],
  ["zhipu", "智谱 GLM", "Zhipu AI GLM"],
  ["modelscope", "魔搭 ModelScope", "ModelScope"],
  ["openai_compatible", "通用 OpenAI 兼容接口", "Generic OpenAI-compatible API"],
];

test("English model provider labels contain no Chinese copy", () => {
  for (const [name, label, expected] of bilingualProviders) {
    const localized = modelProviderLabel({ name, label }, "en");
    assert.equal(localized, expected);
    assert.doesNotMatch(localized, /\p{Script=Han}/u);
  }
});

test("Chinese model provider labels preserve the server catalog", () => {
  for (const [name, label] of bilingualProviders) {
    assert.equal(modelProviderLabel({ name, label }, "zh-CN"), label);
  }
});

test("an unknown provider uses its identifier in English", () => {
  assert.equal(
    modelProviderLabel({ name: "future_provider", label: "未来供应商" }, "en"),
    "future_provider",
  );
});
