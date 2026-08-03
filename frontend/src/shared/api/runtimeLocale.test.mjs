import assert from "node:assert/strict";
import { test } from "node:test";

import {
  localizeRuntimeMessage,
  setRuntimeLocale,
} from "./runtimeLocale.ts";

test("runtime messages resolve from the current locale at display time", () => {
  const message = { zhCN: "中文超时", en: "English timeout" };
  setRuntimeLocale("zh-CN");
  assert.equal(localizeRuntimeMessage(message), message.zhCN);
  setRuntimeLocale("en");
  assert.equal(localizeRuntimeMessage(message), message.en);
  assert.equal(localizeRuntimeMessage("fixed diagnostic"), "fixed diagnostic");
});
