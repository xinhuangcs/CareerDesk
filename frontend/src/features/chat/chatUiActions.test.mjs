import assert from "node:assert/strict";
import test from "node:test";

import { chatUiActionsFromServer } from "./chatUiActions.ts";

test("chat UI actions map only server allowlisted references to local routes", () => {
  assert.deepEqual(chatUiActionsFromServer([
    { kind: "open_application_research", resource_id: 7 },
    { kind: "open_questions" },
    { kind: "open_resume", resource_id: 3 },
    { kind: "open_timeline" },
  ]), [
    {
      kind: "open_application_research", resourceId: 7,
      label: "查看公司与岗位调研", href: "/timeline?application=7&tab=research",
    },
    { kind: "open_questions", resourceId: null, label: "打开题库", href: "/grill?view=questions" },
    { kind: "open_resume", resourceId: 3, label: "打开这份简历", href: "/library?resumeId=3" },
    { kind: "open_timeline", resourceId: null, label: "打开求职进展", href: "/timeline" },
  ]);
});

test("chat UI actions fail closed for unknown, malformed or duplicated actions", () => {
  assert.deepEqual(chatUiActionsFromServer(undefined), []);
  assert.equal(chatUiActionsFromServer([{ kind: "open_url", url: "https://evil.test" }]), null);
  assert.equal(chatUiActionsFromServer([{ kind: "open_application", resource_id: 0 }]), null);
  assert.equal(chatUiActionsFromServer([{ kind: "open_questions", resource_id: 1 }]), null);
  assert.equal(chatUiActionsFromServer([
    { kind: "open_library" }, { kind: "open_library" },
  ]), null);
});
