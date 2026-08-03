import assert from "node:assert/strict";
import test from "node:test";

import { parseGrillDeepLink } from "./grillDeepLink.ts";

test("grill deep links accept only positive ids and explicit custom edition", () => {
  assert.deepEqual(parseGrillDeepLink("?session=12&edition=custom&application=8"), {
    sessionId: 12, applicationId: 8,
  });
  assert.deepEqual(parseGrillDeepLink("?session=-1&application=8"), {
    sessionId: null, applicationId: null,
  });
});
