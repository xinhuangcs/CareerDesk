import assert from "node:assert/strict";
import { test } from "node:test";
import { detectSystemLocale } from "./localePreference.ts";
import { en, zhCN } from "./resources.ts";

test("system locale maps every Chinese variant to Simplified Chinese and defaults others to English", () => {
  assert.equal(detectSystemLocale("zh-CN"), "zh-CN");
  assert.equal(detectSystemLocale("zh-Hant-TW"), "zh-CN");
  assert.equal(detectSystemLocale("en-US"), "en");
  assert.equal(detectSystemLocale("da-DK"), "en");
});

test("Chinese and English resources have exact key and interpolation parity", () => {
  assert.deepEqual(Object.keys(en).sort(), Object.keys(zhCN).sort());
  const placeholders = (value) => [...value.matchAll(/\{\{\s*([^},\s]+)[^}]*\}\}/g)]
    .map((match) => match[1])
    .sort();
  for (const key of Object.keys(zhCN)) {
    assert.notEqual(zhCN[key].trim(), "", `${key} has an empty Chinese value`);
    assert.notEqual(en[key].trim(), "", `${key} has an empty English value`);
    assert.deepEqual(placeholders(en[key]), placeholders(zhCN[key]), `${key} placeholder drift`);
  }
});

test("the English drill navigation label stays shorter than its page title", () => {
  assert.equal(en["shell.nav.grill"], "Drill");
  assert.equal(en["shell.page.grill.title"], "Interview Drill");
});
