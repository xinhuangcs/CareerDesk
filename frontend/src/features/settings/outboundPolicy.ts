export type OutboundPolicy = {
  strict_offline: boolean;
  allow_conversation_embedding: boolean;
  allow_web_research: boolean;
  allow_deep_research: boolean;
  allow_ddg_fallback: boolean;
};

export type OutboundPermission =
  | "allow_conversation_embedding"
  | "allow_web_research";

export type SearchOutletKeys = {
  tavily: boolean;
  brave: boolean;
  google: boolean;
  searxng: boolean;
};

export type OutboundCapabilityKeys = { openai: boolean } & Partial<SearchOutletKeys>;

export type OutboundCapabilityStatus =
  | "disabled"
  | "paused_by_strict_offline"
  | "missing_openai_key"
  | "ready_openai"
  | "ready_search"
  | "ready_duckduckgo_only"
  | "no_search_outlet";

export type OpenAICompatibleEndpointStatus = "configured" | "missing" | "invalid";

export const DEFAULT_OUTBOUND_POLICY: Readonly<OutboundPolicy> = Object.freeze({
  strict_offline: false,
  allow_conversation_embedding: false,
  allow_web_research: false,
  allow_deep_research: false,
  allow_ddg_fallback: true,
});

const PERMISSIONS: readonly OutboundPermission[] = [
  "allow_conversation_embedding",
  "allow_web_research",
];

const POLICY_FIELDS = Object.keys(DEFAULT_OUTBOUND_POLICY) as (keyof OutboundPolicy)[];

export function retainVisibleCredentialChanges<T>(
  changes: Readonly<Record<string, T>>,
  visibleNames: ReadonlySet<string>,
): Record<string, T> {
  return Object.fromEntries(
    Object.entries(changes).filter(([name]) => visibleNames.has(name)),
  ) as Record<string, T>;
}

export function sameOutboundPolicy(left: OutboundPolicy, right: OutboundPolicy): boolean {
  return POLICY_FIELDS.every((field) => left[field] === right[field]);
}

export function enabledOutboundPermissionCount(policy: OutboundPolicy): number {
  return PERMISSIONS.filter((permission) => policy[permission]).length;
}

export function outboundPermissionIsEffective(
  policy: OutboundPolicy,
  permission: OutboundPermission,
): boolean {
  return !policy.strict_offline && policy[permission];
}

export function cloudModelCredentialOptions<T extends { local: boolean; key_vars: readonly string[] }>(
  policy: OutboundPolicy,
  provider: T | null,
): readonly string[] {
  if (policy.strict_offline
    || provider === null
    || provider.local) return [];
  return provider.key_vars;
}

export function strictOfflineChangeRequiresReload(
  previous: OutboundPolicy,
  next: OutboundPolicy,
): boolean {
  return previous.strict_offline !== next.strict_offline;
}

export function configuredSearchOutletNames(
  keys: Partial<SearchOutletKeys>,
  ddgAllowed: boolean,
): string[] {
  const names: string[] = [];
  if (keys.tavily) names.push("Tavily");
  if (keys.brave) names.push("Brave");
  if (keys.google) names.push("Google");
  if (keys.searxng) names.push("SearXNG");
  if (ddgAllowed) names.push("DuckDuckGo");
  return names;
}

export function outboundCapabilityStatus(
  policy: OutboundPolicy,
  permission: OutboundPermission,
  keys: OutboundCapabilityKeys,
): OutboundCapabilityStatus {
  if (!policy[permission]) return "disabled";
  if (policy.strict_offline) return "paused_by_strict_offline";
  if (permission === "allow_web_research") {
    const hasOfficialOutlet = Boolean(keys.tavily || keys.brave || keys.google || keys.searxng);
    if (hasOfficialOutlet) return "ready_search";
    return policy.allow_ddg_fallback ? "ready_duckduckgo_only" : "no_search_outlet";
  }
  return keys.openai ? "ready_openai" : "missing_openai_key";
}

export function outboundCapabilityStatusText(status: OutboundCapabilityStatus, locale: UiLocale = "zh-CN"): string {
  const labels: Record<OutboundCapabilityStatus, string> = {
    disabled: "未启用，不会发送数据",
    paused_by_strict_offline: "已启用，但已被严格离线暂停",
    missing_openai_key: "已启用，但缺少 OpenAI API Key，当前不会发送数据",
    ready_openai: "已启用，可以使用",
    ready_search: "已启用，将使用已配置的搜索服务",
    ready_duckduckgo_only: "已启用，将使用 DuckDuckGo 备用搜索（非官方接口）",
    no_search_outlet: "已启用，但没有可用的搜索服务。请配置搜索 API Key，或允许使用 DuckDuckGo 备用搜索",
  };
  if (locale === "zh-CN") return labels[status];
  const english: Record<OutboundCapabilityStatus, string> = {
    disabled: "Off; no data will be sent",
    paused_by_strict_offline: "On, but paused by strict offline",
    missing_openai_key: "On, but missing an OpenAI API key; no data is currently sent",
    ready_openai: "On and ready",
    ready_search: "On; configured search services will be used",
    ready_duckduckgo_only: "On; DuckDuckGo fallback search will be used (unofficial interface)",
    no_search_outlet: "On, but no search service is available. Configure a search API key or allow DuckDuckGo fallback search",
  };
  return english[status];
}

export function openAICompatibleEndpointSaveError(
  strictOffline: boolean,
  provider: string | null,
  endpointStatus: OpenAICompatibleEndpointStatus,
  locale: UiLocale = "zh-CN",
): string | null {
  if (strictOffline || provider !== "openai_compatible" || endpointStatus === "configured") {
    return null;
  }
  if (endpointStatus === "missing") {
    return locale === "en"
      ? "The generic OpenAI-compatible interface has no service URL. Stop CareerDesk completely, set OPENAI_BASE_URL (preferred) or LLM_BASE_URL in .env or the startup environment, then restart."
      : "通用 OpenAI 兼容接口尚未配置服务地址。请先完全停止 CareerDesk，在 .env 或启动环境设置 OPENAI_BASE_URL（优先）或 LLM_BASE_URL，再重新启动。";
  }
  return locale === "en"
    ? "The generic OpenAI-compatible interface has an invalid service URL. Stop CareerDesk completely, correct it to an HTTP(S) URL without credentials, query parameters, or fragments, then restart."
    : "通用 OpenAI 兼容接口的服务地址无效。请先完全停止 CareerDesk，将其修正为不含凭据、查询参数或片段标识的 HTTP(S) URL，再重新启动。";
}
import type { UiLocale } from "../../i18n/i18n";
