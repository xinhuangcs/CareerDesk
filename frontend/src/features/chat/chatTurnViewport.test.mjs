import assert from "node:assert/strict";
import test from "node:test";
import {
  collapseTurnReservation,
  latestTurnTopInset,
  minimumConversationLeadSpacerHeight,
  reanchoredTurnMaxScrollY,
  stableTurnScrollY,
  turnSpacerHeightForMaxScroll,
  turnSpacerHeightWithinCollapseLimit,
  wheelTurnCollapseDistance,
} from "./chatTurnViewport.ts";

test("a viewport-height probe finds the exact space needed to anchor a short turn", () => {
  assert.equal(turnSpacerHeightForMaxScroll(720, 420, 790), 350);
  assert.equal(turnSpacerHeightForMaxScroll(720, 420, 1_180), 0);
});

test("streamed output consumes reserved space before extending the page", () => {
  assert.equal(turnSpacerHeightForMaxScroll(460, 720, 890), 290);
  assert.equal(turnSpacerHeightForMaxScroll(80, 720, 860), 0);
});

test("layout recalculation cannot restore user-collapsed turn space", () => {
  assert.equal(turnSpacerHeightWithinCollapseLimit(401, null), 401);
  assert.equal(turnSpacerHeightWithinCollapseLimit(401, 301), 301);
  assert.equal(turnSpacerHeightWithinCollapseLimit(220, 301), 220);
  assert.equal(turnSpacerHeightWithinCollapseLimit(401, Number.NaN), 0);
});

test("fractional turn anchors use one stable browser scroll pixel", () => {
  assert.equal(stableTurnScrollY(386.49), 386);
  assert.equal(stableTurnScrollY(386.5), 387);
  assert.equal(stableTurnScrollY(Number.NaN), 0);
  assert.equal(reanchoredTurnMaxScrollY(386.5, 446.5, 60, 446.5, 60), 387);
});

test("the first turn keeps only the lead needed to clear fixed controls", () => {
  assert.equal(minimumConversationLeadSpacerHeight(52, 60), 8);
  assert.equal(minimumConversationLeadSpacerHeight(104, 64), 0);
  assert.equal(minimumConversationLeadSpacerHeight(Number.NaN, 60), 0);
});

test("wheel collapse distance is consistent across browser delta modes", () => {
  assert.equal(wheelTurnCollapseDistance(-120, 0, 720), 120);
  assert.equal(wheelTurnCollapseDistance(-3, 1, 720), 120);
  assert.equal(wheelTurnCollapseDistance(-1, 2, 720), 720);
  assert.equal(wheelTurnCollapseDistance(120, 0, 720), 0);
});

test("upstream layout changes move the anchor without restoring collapsed space", () => {
  assert.equal(reanchoredTurnMaxScrollY(423, 447, 24, 572, 24), 548);
  assert.equal(reanchoredTurnMaxScrollY(300, 447, 24, 572, 24), 425);
  assert.equal(reanchoredTurnMaxScrollY(423, 447, 24, 447, 64), 383);
});

test("moving message cards down collapses the unused space permanently", () => {
  const initial = { maxScrollY: 720, spacerHeight: 300 };
  const halfway = collapseTurnReservation(initial, 720, 600);
  assert.deepEqual(halfway, { maxScrollY: 600, spacerHeight: 180 });

  const movedBackUp = collapseTurnReservation(halfway, 600, 680);
  assert.deepEqual(movedBackUp, halfway);

  assert.deepEqual(
    collapseTurnReservation(halfway, 600, 300),
    { maxScrollY: 420, spacerHeight: 0 },
  );
});

test("the latest turn stays below the global chat controls and mobile navigation", () => {
  assert.equal(latestTurnTopInset(767), 64);
  assert.equal(latestTurnTopInset(768), 60);
});
