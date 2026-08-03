import assert from "node:assert/strict";
import test from "node:test";

import { isPreferenceItemCommandStatus } from "./preferenceItemCommandContract.ts";

const commandId = "00000000-0000-4000-8000-000000000001";
const operationId = "00000000-0000-4000-8000-000000000002";
const target = { id: 7, revision: 3 };
const setCommand = { action: "set", target };

function completed(outcome = "updated") {
  return {
    contract_version: 1,
    command_id: commandId,
    state: "completed",
    action: outcome === "deleted" ? "delete" : "set",
    target,
    result: {
      outcome,
      before: target,
      final: outcome === "deleted" ? null : outcome === "updated" ? { id: 7, revision: 4 } : target,
    },
    error: null,
    operation_id: outcome === "no_change" ? null : operationId,
    finished_time: "2026-07-14T12:00:00.123456+00:00",
  };
}

test("completed update, delete and no-change statuses enforce exact terminal identities", () => {
  assert.equal(isPreferenceItemCommandStatus(completed(), commandId, setCommand), true);
  assert.equal(isPreferenceItemCommandStatus(
    completed("deleted"), commandId, { action: "delete", target },
  ), true);
  assert.equal(isPreferenceItemCommandStatus(completed("no_change"), commandId, setCommand), true);

  const wrongRevision = completed();
  wrongRevision.result.final.revision = 5;
  assert.equal(isPreferenceItemCommandStatus(wrongRevision, commandId, setCommand), false);
  const deleteAsSet = completed("deleted");
  assert.equal(isPreferenceItemCommandStatus(deleteAsSet, commandId, setCommand), false);
  const noChangeWithOperation = completed("no_change");
  noChangeWithOperation.operation_id = operationId;
  assert.equal(isPreferenceItemCommandStatus(noChangeWithOperation, commandId, setCommand), false);
});

test("rejected and cancelled are strict 2xx terminal DTOs", () => {
  const rejected = {
    ...completed(),
    state: "rejected",
    result: null,
    operation_id: null,
    error: { code: "target_changed", message: "偏好已经变化" },
  };
  assert.equal(isPreferenceItemCommandStatus(rejected, commandId, setCommand), true);
  for (const code of ["target_missing", "target_changed", "limit_exceeded", "projection_invalid"]) {
    assert.equal(isPreferenceItemCommandStatus(
      { ...rejected, error: { code, message: "安全拒绝" } }, commandId, setCommand,
    ), true);
  }
  const cancelled = {
    ...rejected,
    state: "cancelled",
    error: null,
  };
  assert.equal(isPreferenceItemCommandStatus(cancelled, commandId, setCommand), true);
  assert.equal(isPreferenceItemCommandStatus(
    { ...cancelled, error: { code: "target_changed", message: "x" } }, commandId, setCommand,
  ), false);
});

test("unknown fields, values, mismatched commands and noncanonical timestamps fail closed", () => {
  assert.equal(isPreferenceItemCommandStatus(
    { ...completed(), value: "secret" }, commandId, setCommand,
  ), false);
  assert.equal(isPreferenceItemCommandStatus(completed(), operationId, setCommand), false);
  assert.equal(isPreferenceItemCommandStatus(
    { ...completed(), target: { id: 8, revision: 3 } }, commandId, setCommand,
  ), false);
  for (const finished_time of [
    "2026-07-14T12:00:00Z",
    "2026-07-14T12:00:00.000000+00:00",
    "2026-02-30T12:00:00+00:00",
  ]) {
    assert.equal(isPreferenceItemCommandStatus(
      { ...completed(), finished_time }, commandId, setCommand,
    ), false);
  }
});
