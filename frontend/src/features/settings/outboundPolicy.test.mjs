import assert from "node:assert/strict";
import { test } from "node:test";

import {
  DEFAULT_OUTBOUND_POLICY,
  cloudModelCredentialOptions,
  configuredSearchOutletNames,
  enabledOutboundPermissionCount,
  outboundCapabilityStatus,
  outboundCapabilityStatusText,
  outboundPermissionIsEffective,
  openAICompatibleEndpointSaveError,
  retainVisibleCredentialChanges,
  sameOutboundPolicy,
  strictOfflineChangeRequiresReload,
} from "./outboundPolicy.ts";

test("all optional outbound capabilities are disabled by default", () => {
  assert.deepEqual(DEFAULT_OUTBOUND_POLICY, {
    strict_offline: false,
    allow_conversation_embedding: false,
    allow_web_research: false,
    allow_deep_research: false,
    allow_ddg_fallback: true,
  });
  assert.equal(enabledOutboundPermissionCount(DEFAULT_OUTBOUND_POLICY), 0);
});

test("configured keys never grant outbound permission", () => {
  assert.equal(outboundCapabilityStatus(
    DEFAULT_OUTBOUND_POLICY,
    "allow_conversation_embedding",
    { openai: true, tavily: true },
  ), "disabled");
  assert.equal(outboundCapabilityStatus(
    DEFAULT_OUTBOUND_POLICY,
    "allow_web_research",
    { openai: true, tavily: true },
  ), "disabled");
});

test("strict offline pauses but does not erase stored permissions", () => {
  const policy = {
    ...DEFAULT_OUTBOUND_POLICY,
    strict_offline: true,
    allow_conversation_embedding: true,
  };
  assert.equal(enabledOutboundPermissionCount(policy), 1);
  assert.equal(outboundPermissionIsEffective(policy, "allow_conversation_embedding"), false);
  assert.equal(outboundCapabilityStatus(
    policy,
    "allow_conversation_embedding",
    { openai: true, tavily: false },
  ), "paused_by_strict_offline");
  assert.equal(policy.allow_conversation_embedding, true);
});

test("turning strict offline off restores only previously granted capabilities", () => {
  const strict = {
    ...DEFAULT_OUTBOUND_POLICY,
    strict_offline: true,
    allow_conversation_embedding: true,
    allow_web_research: true,
  };
  const resumed = { ...strict, strict_offline: false };
  assert.equal(outboundPermissionIsEffective(resumed, "allow_conversation_embedding"), true);
  assert.equal(outboundPermissionIsEffective(resumed, "allow_web_research"), true);
  assert.equal(sameOutboundPolicy(strict, resumed), false);
  assert.equal(sameOutboundPolicy(resumed, { ...resumed }), true);
});

test("OpenAI embedding consent and credential availability remain independent", () => {
  const policy = {
    ...DEFAULT_OUTBOUND_POLICY,
    allow_conversation_embedding: true,
  };
  assert.equal(outboundCapabilityStatus(
    policy,
    "allow_conversation_embedding",
    { openai: false, tavily: false },
  ), "missing_openai_key");
  assert.equal(outboundCapabilityStatus(
    policy,
    "allow_conversation_embedding",
    { openai: true, tavily: false },
  ), "ready_openai");
});

test("web research outlets follow configured keys and the explicit ddg fallback switch", () => {
  const policy = {
    ...DEFAULT_OUTBOUND_POLICY,
    allow_web_research: true,
  };
  assert.equal(outboundCapabilityStatus(
    policy,
    "allow_web_research",
    { openai: false, tavily: false, brave: false, google: false, searxng: false },
  ), "ready_duckduckgo_only");
  assert.equal(outboundCapabilityStatus(
    policy,
    "allow_web_research",
    { openai: false, tavily: false, brave: true, google: false, searxng: false },
  ), "ready_search");
  assert.equal(outboundCapabilityStatus(
    { ...policy, allow_ddg_fallback: false },
    "allow_web_research",
    { openai: false, tavily: false, brave: false, google: false, searxng: false },
  ), "no_search_outlet");
  assert.match(
    outboundCapabilityStatusText("ready_duckduckgo_only"),
    /DuckDuckGo.*非官方/,
  );
  assert.doesNotMatch(
    outboundCapabilityStatusText("ready_search"),
    /SerpAPI/,
  );
  assert.equal(
    outboundCapabilityStatusText("ready_search"),
    "已启用，将使用已配置的搜索服务",
  );
  assert.deepEqual(
    configuredSearchOutletNames({ tavily: true, brave: true, google: false, searxng: false }, true),
    ["Tavily", "Brave", "DuckDuckGo"],
  );
  assert.equal(sameOutboundPolicy(policy, { ...policy, allow_ddg_fallback: false }), false);
});


test("cloud providers expose every accepted credential alias", () => {
  const gemini = {
    local: false,
    key_vars: ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
  };
  assert.deepEqual(
    cloudModelCredentialOptions(DEFAULT_OUTBOUND_POLICY, gemini),
    ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
  );
  assert.deepEqual(cloudModelCredentialOptions({
    ...DEFAULT_OUTBOUND_POLICY,
    strict_offline: true,
  }, gemini), []);
});

test("changing strict offline requires a document reload for CSP", () => {
  assert.equal(strictOfflineChangeRequiresReload(
    DEFAULT_OUTBOUND_POLICY,
    { ...DEFAULT_OUTBOUND_POLICY, strict_offline: true },
  ), true);
  assert.equal(strictOfflineChangeRequiresReload(
    DEFAULT_OUTBOUND_POLICY,
    { ...DEFAULT_OUTBOUND_POLICY, allow_web_research: true },
  ), false);
});

test("active OpenAI-compatible models require a valid configured endpoint", () => {
  assert.match(
    openAICompatibleEndpointSaveError(false, "openai_compatible", "missing"),
    /OPENAI_BASE_URL.*LLM_BASE_URL/,
  );
  assert.match(
    openAICompatibleEndpointSaveError(false, "openai_compatible", "invalid"),
    /服务地址无效/,
  );
  assert.equal(
    openAICompatibleEndpointSaveError(false, "openai_compatible", "configured"),
    null,
  );
  assert.equal(
    openAICompatibleEndpointSaveError(false, "anthropic", "missing"),
    null,
  );
});

test("strict offline keeps an incomplete OpenAI-compatible model dormant", () => {
  assert.equal(
    openAICompatibleEndpointSaveError(true, "openai_compatible", "missing"),
    null,
  );
  assert.equal(
    openAICompatibleEndpointSaveError(true, "openai_compatible", "invalid"),
    null,
  );
});

test("hidden credential changes cannot survive a provider switch", () => {
  const pendingClears = {
    ANTHROPIC_API_KEY: true,
    OPENAI_API_KEY: true,
    TAVILY_API_KEY: false,
  };

  assert.deepEqual(
    retainVisibleCredentialChanges(
      pendingClears,
      new Set(["OPENAI_API_KEY", "TAVILY_API_KEY"]),
    ),
    {
      OPENAI_API_KEY: true,
      TAVILY_API_KEY: false,
    },
  );
});
