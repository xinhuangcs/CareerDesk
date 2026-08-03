import assert from "node:assert/strict";
import test from "node:test";

import {
  preferenceValueCodePointLength,
  preferenceValueValidationIssue,
  reconcilePreferenceItemCommand,
} from "./preferenceItemCommandState.ts";

const target = { id: 7, revision: 3 };
const item = {
  id: 7,
  key: "目标城市",
  value: "哥本哈根",
  revision: 4,
  created_time: "2026-07-14T10:00:00+00:00",
  updated_time: "2026-07-14T12:00:00+00:00",
};
const baseStatus = {
  contract_version: 1,
  command_id: "00000000-0000-4000-8000-000000000001",
  state: "completed",
  action: "set",
  target,
  result: { outcome: "updated", before: target, final: { id: 7, revision: 4 } },
  error: null,
  operation_id: "00000000-0000-4000-8000-000000000002",
  finished_time: "2026-07-14T12:00:00+00:00",
};
function snapshot(items) {
  return {
    items,
    total: items.length,
    total_chars: items.reduce((total, current) => total
      + Array.from(current.key).length + Array.from(current.value).length, 0),
    recovery_scope: "a".repeat(64),
  };
}

test("value validation uses Unicode code points and rejects hidden normalization", () => {
  assert.equal(preferenceValueCodePointLength("😀a"), 2);
  assert.equal(preferenceValueValidationIssue("", "old"), "偏好值不能为空。");
  assert.equal(preferenceValueValidationIssue(" value", "old"), "请删除偏好值首尾的空白字符。");
  assert.equal(preferenceValueValidationIssue("same", "same"), "内容没有变化。");
  assert.equal(preferenceValueValidationIssue("值".repeat(2_001), "old"), "偏好值最多 2000 个字符。");
  assert.equal(preferenceValueValidationIssue("new", "old"), null);
});

test("terminal update distinguishes current, later change and impossible rollback", () => {
  assert.deepEqual(reconcilePreferenceItemCommand(baseStatus, snapshot([item])), {
    valid: true,
    current: true,
    message: "偏好已更新。",
  });
  const later = { ...item, revision: 5, value: "奥胡斯" };
  assert.equal(reconcilePreferenceItemCommand(baseStatus, snapshot([later])).current, false);
  const older = { ...item, revision: 3 };
  assert.equal(reconcilePreferenceItemCommand(baseStatus, snapshot([older])).valid, false);
});

test("delete identifies only the frozen row, even when the same key is recreated", () => {
  const deleted = {
    ...baseStatus,
    action: "delete",
    result: { outcome: "deleted", before: target, final: null },
  };
  const recreated = { ...item, id: 8, revision: 1 };
  const result = reconcilePreferenceItemCommand(deleted, snapshot([recreated]));
  assert.equal(result.valid, true);
  assert.match(result.message, /原偏好已删除/);
  assert.equal(reconcilePreferenceItemCommand(deleted, snapshot([item])).valid, false);
});
