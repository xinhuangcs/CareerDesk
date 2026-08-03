import assert from "node:assert/strict";
import { test } from "node:test";
import {
  grillExperimentIntroWasSeen,
  grillNavigationIsVisible,
  markGrillExperimentIntroSeen,
  saveGrillNavigationVisibility,
} from "./grillVisibilityPreference.ts";

function memoryStorage(initial = {}) {
  const values = new Map(Object.entries(initial));
  return {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
  };
}

test("grill stays visible by default and only the exact hidden value removes navigation", () => {
  assert.equal(grillNavigationIsVisible(memoryStorage()), true);
  assert.equal(grillNavigationIsVisible(memoryStorage({
    "careerdesk.grill.navigation.v1": "hidden",
  })), false);
  assert.equal(grillNavigationIsVisible(memoryStorage({
    "careerdesk.grill.navigation.v1": "unexpected",
  })), true);
});

test("grill visibility persists before notifying the mounted app shell", () => {
  const storage = memoryStorage();
  const observed = [];

  assert.equal(saveGrillNavigationVisibility(false, storage, () => {
    observed.push(grillNavigationIsVisible(storage));
  }), true);
  assert.deepEqual(observed, [false]);
  assert.equal(grillNavigationIsVisible(storage), false);
});

test("the experiment introduction is tracked independently for each release", () => {
  const storage = memoryStorage({
    "careerdesk.grill.experiment-intro.v1": "seen",
  });

  assert.equal(grillExperimentIntroWasSeen("1.0.0", storage), false);
  assert.equal(markGrillExperimentIntroSeen("1.0.0", storage), true);
  assert.equal(grillExperimentIntroWasSeen("1.0.0", storage), true);
  assert.equal(grillExperimentIntroWasSeen("1.1.0", storage), false);
  assert.equal(markGrillExperimentIntroSeen("1.1.0", storage), true);
  assert.equal(grillExperimentIntroWasSeen("1.1.0", storage), true);
});

test("blocked browser storage fails safely without hiding navigation", () => {
  const storage = {
    getItem: () => { throw new Error("blocked"); },
    setItem: () => { throw new Error("blocked"); },
  };

  assert.equal(grillNavigationIsVisible(storage), true);
  assert.equal(grillExperimentIntroWasSeen("1.0.0", storage), false);
  assert.equal(saveGrillNavigationVisibility(false, storage, () => assert.fail()), false);
  assert.equal(markGrillExperimentIntroSeen("1.0.0", storage), false);
});
