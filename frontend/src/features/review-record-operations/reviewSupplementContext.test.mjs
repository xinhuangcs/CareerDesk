import assert from "node:assert/strict";
import { test } from "node:test";

import {
  MAX_CHAT_MESSAGE_CHARS,
  REVIEW_SUPPLEMENT_VISIBLE_PROMPT,
  REVIEW_SUPPLEMENT_VISIBLE_PROMPT_EN,
  removeReviewSupplementComposerPrompt,
  reviewSupplementComposerText,
  reviewSupplementRequestFields,
} from "./reviewSupplementContext.ts";

const referenceA = "00000000-0000-4000-8000-000000000001";
const referenceB = "00000000-0000-4000-8000-000000000002";

test("the visible supplement composer never exposes an opaque review reference", () => {
  const visible = reviewSupplementComposerText("补充回答");
  assert.equal(visible, `${REVIEW_SUPPLEMENT_VISIBLE_PROMPT}\n补充回答`);
  assert.equal(visible.includes(referenceA), false);
  assert.equal(reviewSupplementComposerText(visible), visible);
});

test("switching trusted references does not duplicate visible supplement prefixes", () => {
  const visible = reviewSupplementComposerText(reviewSupplementComposerText("回答"));
  assert.equal(visible.match(new RegExp(REVIEW_SUPPLEMENT_VISIBLE_PROMPT, "g"))?.length, 1);
  assert.equal(visible.includes(referenceA), false);
  assert.equal(visible.includes(referenceB), false);
});

test("the English composer and attachment placeholder stay English", () => {
  const visible = reviewSupplementComposerText("补充回答", "en");
  assert.equal(visible, `${REVIEW_SUPPLEMENT_VISIBLE_PROMPT_EN}\n补充回答`);
  assert.equal(removeReviewSupplementComposerPrompt(visible), "补充回答");
  assert.deepEqual(reviewSupplementRequestFields("", referenceA, "en"), {
    message: "(see attachment)",
    review_supplement_reference: referenceA,
  });
});

test("the request keeps the natural supplement body and binds the exact reference separately", () => {
  const visible = reviewSupplementComposerText("回答");
  const supplementText = removeReviewSupplementComposerPrompt(visible);
  const request = reviewSupplementRequestFields(supplementText, referenceB);
  assert.deepEqual(request, {
    message: "回答",
    review_supplement_reference: referenceB,
  });
  assert.equal(request.message.includes(referenceB), false);
  assert.equal(request.message.includes("<careerdesk_"), false);
  assert.throws(() => reviewSupplementRequestFields(supplementText, "not-a-uuid"));
});

test("the structured reference does not consume the 50,000 character message budget", () => {
  const exactLimit = "字".repeat(MAX_CHAT_MESSAGE_CHARS);
  const request = reviewSupplementRequestFields(exactLimit, referenceA);
  assert.equal(request.message.length, MAX_CHAT_MESSAGE_CHARS);
  assert.equal(request.review_supplement_reference, referenceA);
  assert.doesNotThrow(() => reviewSupplementRequestFields(
    "🙂".repeat(MAX_CHAT_MESSAGE_CHARS),
    referenceA,
  ));
  assert.throws(
    () => reviewSupplementRequestFields(`${exactLimit}字`, referenceA),
    /50[,.]?000/,
  );
});

test("an attachment-only supplement uses a natural body placeholder", () => {
  assert.deepEqual(reviewSupplementRequestFields("", referenceA), {
    message: "（见附件）",
    review_supplement_reference: referenceA,
  });
});
