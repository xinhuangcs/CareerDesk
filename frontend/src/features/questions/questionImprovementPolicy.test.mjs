import assert from "node:assert/strict";
import test from "node:test";

import { canOpenQuestionImprovement } from "./questionImprovementPolicy.ts";

test("only generated questions marked bad may open the improvement panel", () => {
  assert.equal(canOpenQuestionImprovement("generated", "bad"), true);

  for (const qualityFlag of [null, "good"]) {
    assert.equal(canOpenQuestionImprovement("generated", qualityFlag), false);
  }

  for (const source of ["real", "imported"]) {
    for (const qualityFlag of [null, "good", "bad"]) {
      assert.equal(canOpenQuestionImprovement(source, qualityFlag), false);
    }
  }
});
