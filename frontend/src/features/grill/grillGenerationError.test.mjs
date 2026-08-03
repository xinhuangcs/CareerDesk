import assert from "node:assert/strict";
import { test } from "node:test";

import { generationErrorMessage, readinessErrorMessage } from "./grillGenerationError.ts";

test("generation failures render actionable Chinese instead of raw internal codes", () => {
  assert.equal(
    generationErrorMessage("unsupported_category"),
    "旧任务包含通用练习不支持的题目类别，请重新生成",
  );
  assert.equal(
    generationErrorMessage("model_timeout"),
    "模型生成超过 5 分钟仍未完成，请重试或更换响应更快的模型",
  );
  assert.equal(
    generationErrorMessage("future_safe_code"),
    "题集生成失败，请重试",
  );
  assert.equal(
    generationErrorMessage("future_safe_code", "future_safe_code"),
    "题集生成失败，请重试",
  );
  assert.equal(
    generationErrorMessage("future_safe_code", "服务暂时不可用，请重试"),
    "服务暂时不可用，请重试",
  );
});

test("readiness errors are localized from stable codes", () => {
  assert.equal(readinessErrorMessage("missing_jd", "en"), "This role has no job description. Add it in role details first.");
  assert.equal(readinessErrorMessage("future_code", "en"), "The selected source material is not ready. Review the requirements above.");
});
