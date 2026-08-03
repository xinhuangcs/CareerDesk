import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { ChatAssistantProgress } from "./ChatAssistantProgress.ts";

const source = readFileSync(new URL("./ChatPage.tsx", import.meta.url), "utf8");
const appSource = readFileSync(new URL("../../app/App.tsx", import.meta.url), "utf8");
const viewportSource = readFileSync(new URL("./useChatTurnViewport.ts", import.meta.url), "utf8");

test("chat clears Tool progress when the first answer delta arrives", () => {
  assert.match(
    source,
    /ev\.event === "message_delta"[\s\S]*?setToolStatus\(null\)[\s\S]*?item\.text \+ ev\.data\.text/,
  );
});

test("ordinary assistant waiting stays visual and does not show generic AI copy", () => {
  assert.doesNotMatch(source, /正在启动助手|正在处理请求|正在思考|Starting assistant|Processing request|Thinking/);
  assert.match(
    source,
    /aria-label=\{l\("助手正在生成回复", "Assistant response in progress"\)\}/,
  );
  assert.match(source, /<span aria-hidden className="inline-block h-1\.5/);
});

test("stop requests server cancellation and discards cancelled recovery state", () => {
  assert.match(source, /async function stop\(\)/);
  assert.match(source, /await cancelChatTurn\(inFlight\.clientTurnId\)/);
  assert.doesNotMatch(source, /function stop\(\) \{[\s\S]*?abortRef\.current\?\.abort/);
  assert.match(
    source,
    /explicitlyCancelled = stopRequestedTurnIdRef\.current === clientTurnId[\s\S]*streamError\?\.code === "turn_cancelled"/,
  );
  assert.match(source, /discardCancelledTrustedOperationTurn\(clientTurnId, false\)/);
  assert.match(source, /reason instanceof HttpError && reason\.code === "turn_finalizing"/);
  assert.match(
    source,
    /cancelAcceptedTurnIdRef\.current === clientTurnId[\s\S]*?ev\.event !== "done" && ev\.event !== "error"/,
  );
  assert.match(source, /setError\(null\);\s*setToolStatus\(null\);/);
  assert.match(source, /取消本轮并丢弃未确认结果/);
  assert.match(source, /stopping[\s\S]*?animate-spin/);
});

test("new-topic reset clears proposal ownership state and its synchronous ref", () => {
  assert.match(
    source,
    /proposalOperationsRef\.current = \[\];[\s\S]*?proposalOperationTurnIdsRef\.current = \{\};[\s\S]*?setProposalOperationTurnIds\(\{\}\)/,
  );
});

test("settled proposal owners leave the parent coordinator after the two-second notice", () => {
  assert.match(source, /const PROPOSAL_NOTICE_DURATION_MS = 2000/);
  assert.match(
    source,
    /settleProposalOperation[\s\S]*?window\.setTimeout[\s\S]*?proposalOperationsRef\.current = proposalOperationsRef\.current\.filter[\s\S]*?delete nextTurnIds\[key\][\s\S]*?PROPOSAL_NOTICE_DURATION_MS/,
  );
});

test("new-topic actions stay in a fixed top-right control instead of the message flow", () => {
  assert.match(source, /data-chat-new-topic-control/);
  assert.match(source, /const newTopicControl = active && \(/);
  assert.match(source, /className="fixed right-4 top-2\.5 z-30 md:right-8 md:top-6"/);
  assert.match(source, /aria-expanded=\{hasConversationContent \? confirmClear : undefined\}/);
  assert.match(source, /if \(!hasConversationContent\)[\s\S]*?inputRef\.current\?\.focus/);
  assert.match(source, /window\.innerWidth >= 768[\s\S]*?inputRef\.current\?\.focus/);
  assert.match(source, /role="dialog"/);
  assert.match(source, /newTopicButtonRef\.current\?\.focus/);
  assert.doesNotMatch(source, /className="flex items-center justify-end gap-2"/);
  assert.match(appSource, /isChatRoute \? "mr-24" : ""/);
});

test("empty and docked layouts do not reuse imperative spacer state", () => {
  assert.match(source, /<Fragment key="conversation-chat-layout">/);
  assert.match(source, /<Fragment key="empty-chat-layout">/);
  assert.match(source, /key=\{docked \? "docked-chat-composer" : "empty-chat-composer"\}/);
});

test("the conversation composer stays viewport-docked until a new topic restores the empty layout", () => {
  assert.match(source, /docked[\s\S]*?"fixed bottom-0 z-20/);
  assert.doesNotMatch(source, /"sticky bottom-0/);
  assert.match(source, /ref=\{composerReservationRef\} data-chat-composer-reservation/);
  assert.match(viewportSource, /composerDocked: boolean/);
  assert.match(
    viewportSource,
    /syncComposerDock[\s\S]*?reservation\.getBoundingClientRect\(\)[\s\S]*?composer\.style\.left[\s\S]*?composer\.style\.width[\s\S]*?reservation\.style\.height/,
  );
  assert.match(viewportSource, /mainContainer = main\?\.parentElement[\s\S]*?observer\.observe\(mainContainer\)/);
  assert.match(source, /resetTurnViewport\(true\);[\s\S]*?setMessages\(\[\]\)/);
});

test("unused turn space collapses without creating an empty history prefix", () => {
  assert.match(viewportSource, /minimumConversationLeadSpacerHeight/);
  assert.doesNotMatch(viewportSource, /dockedComposerTop/);
  assert.match(viewportSource, /wheelTurnCollapseDistance\([\s\S]*?collapseTurnSpaceBy\(collapseDistance\)[\s\S]*?event\.preventDefault\(\)/);
  assert.match(viewportSource, /event\.target instanceof HTMLTextAreaElement[\s\S]*?event\.target\.scrollTop > 0/);
  assert.match(viewportSource, /touchmove", noteTouchIntent, \{ passive: false \}/);
});

test("proposal-ready events reveal the confirmation surface without a transient label", () => {
  assert.match(
    source,
    /ev\.data\.tool === "proposal_ready"[\s\S]*?revealProposalOperation\(proposal, clientTurnId\);[\s\S]*?setToolStatus\(null\)/,
  );
  assert.match(
    source,
    /if \(ev\.data\.tool !== "proposal_ready"\) \{[\s\S]*?setToolStatus\(ev\.data\.label\)/,
  );
});

test("a Tool started after model preamble text renders its processing spinner", () => {
  const html = renderToStaticMarkup(createElement(
    "div",
    null,
    createElement("p", null, "Model preamble"),
    createElement(ChatAssistantProgress, {
      busy: true,
      messageId: "turn-1:assistant",
      clientTurnId: "turn-1",
      label: "Updating the application…",
      afterText: true,
    }),
  ));

  assert.match(html, /Model preamble/);
  assert.match(html, /role="status"/);
  assert.match(html, /animate-spin/);
  assert.match(html, /mt-2/);
  assert.match(html, /Updating the application…/);
});

test("assistant progress disappears after streaming starts or the turn changes", () => {
  const render = (props) => renderToStaticMarkup(
    createElement(ChatAssistantProgress, props),
  );

  assert.equal(render({
    busy: true,
    messageId: "turn-1:assistant",
    clientTurnId: "turn-1",
    label: null,
  }), "");
  assert.equal(render({
    busy: true,
    messageId: "turn-1:assistant",
    clientTurnId: "turn-2",
    label: "Updating…",
  }), "");
});
