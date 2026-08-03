import { useEffect, useState } from "react";
import { useSearchParams } from "react-router-dom";
import { useLocalizer } from "../../i18n/useLocalizer";
import { useLocale } from "../../i18n/localePreference";
import { getJson, HttpError, postJson, putJson } from "../../shared/api/transport";
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
  strictOfflineChangeRequiresReload,
  type OpenAICompatibleEndpointStatus,
  type OutboundPermission,
  type OutboundPolicy,
} from "./outboundPolicy";

const SEARCH_CREDENTIAL_NAMES = [
  "TAVILY_API_KEY",
  "BRAVE_API_KEY",
  "GOOGLE_PSE_API_KEY",
  "GOOGLE_PSE_ENGINE_ID",
  "SEARXNG_BASE_URL",
] as const;
import { PreferencesSettingsSection } from "../preferences/PreferencesSettingsSection";
import { GrillVisibilitySettingsSection } from "../grill/GrillVisibilitySettingsSection";
import { ThemeSettingsSection } from "../theme/ThemeSettingsSection";
import { LanguageSettingsSection } from "./LanguageSettingsSection";
import { useSettingsT } from "./settingsCopy";
import { StorageSettingsSection } from "./StorageSettingsSection";
import { modelProviderLabel } from "./modelProviderLabels";

type ProviderInfo = {
  name: string;
  label: string;
  default_model: string | null;
  key_vars: string[];
  local: boolean;
  context_window: number | null;
  max_output_tokens: number | null;
};

type ModelCapabilities = {
  context_window: number | null;
  max_output_tokens: number | null;
};

type SettingsState = {
  editable: boolean;
  llm_model: string | null;
  llm_model_local: boolean | null;
  llm_capabilities: ModelCapabilities & {
    source: "provider" | "configured" | "missing" | null;
  };
  keys: Record<string, boolean>;
  credential_storage: {
    kind: "system" | "configuration_file" | "server_environment";
    available: boolean;
    label: string;
    issue: string | null;
  };
  providers: ProviderInfo[];
  outbound_policy: OutboundPolicy;
  environment_managed: {
    llm_model: boolean;
    llm_capabilities: Record<keyof ModelCapabilities, boolean>;
    keys: Record<string, boolean>;
    outbound_policy: Record<keyof OutboundPolicy, boolean>;
  };
  openai_compatible_endpoint: {
    status: OpenAICompatibleEndpointStatus;
    url: string | null;
    source: "OPENAI_BASE_URL" | "LLM_BASE_URL" | null;
    externally_managed: boolean;
    issue: string | null;
  };
  revision: string;
  persistence_warning: string | null;
};

type SettingsUpdate = {
  revision: string;
  llm_model?: string | null;
  llm_capabilities?: ModelCapabilities;
  keys?: Record<string, string | null>;
  outbound_policy?: OutboundPolicy;
};

type SettingsSaveScope = "privacy" | "model" | "retrieval" | "research";

const POLICY_FIELDS_BY_SCOPE: Record<SettingsSaveScope, readonly (keyof OutboundPolicy)[]> = {
  privacy: ["strict_offline"],
  model: [],
  retrieval: ["allow_conversation_embedding"],
  research: ["allow_web_research", "allow_deep_research", "allow_ddg_fallback"],
};

const EMPTY_CREDENTIAL_DRAFTS = (): Record<SettingsSaveScope, Record<string, string>> => ({
  privacy: {},
  model: {},
  retrieval: {},
  research: {},
});

const EMPTY_CREDENTIAL_CLEARS = (): Record<SettingsSaveScope, Record<string, boolean>> => ({
  privacy: {},
  model: {},
  retrieval: {},
  research: {},
});

const LOCAL_GUIDES_ZH: Record<string, string> = {
  ollama:
    "先安装 Ollama 并拉取模型（如 ollama pull qwen3），型号填写已拉取的名称；服务默认运行在本机 11434 端口，无需 API Key。",
  vllm:
    "需本机先用 vLLM 启动 OpenAI 兼容服务。注意 vLLM 默认端口 8000 与 CareerDesk 相同：请换端口启动 CareerDesk" +
    "（PORT=8001 uv run --project backend python run.py），或改选「通用 OpenAI 兼容接口」并在 .env 里配 LLM_BASE_URL 指向 vLLM 地址。",
  sglang: "请先在本机启动 SGLang 服务（默认端口 30000），型号填写已加载的模型名；无需 API Key。",
};
const LOCAL_GUIDES_EN: Record<string, string> = {
  ollama: "Install Ollama and pull a model (for example, ollama pull qwen3), then enter the pulled name. The local service defaults to port 11434 and needs no API key.",
  vllm: "Start a local OpenAI-compatible vLLM service first. Its default port 8000 conflicts with CareerDesk; run CareerDesk on another port, or use Generic OpenAI-compatible and point LLM_BASE_URL at vLLM.",
  sglang: "Start SGLang locally (default port 30000), then enter the loaded model name. No API key is required.",
};

function KeyField(props: {
  label: string;
  hint?: string;
  placeholder?: string;
  configuredPlaceholder?: string;
  configured: boolean;
  draft: string;
  pendingClear: boolean;
  disabled: boolean;
  managedByEnvironment: boolean;
  onDraft: (value: string) => void;
  onToggleClear: () => void;
}) {
  const l = useLocalizer();
  const {
    label, hint, placeholder, configuredPlaceholder, configured, draft, pendingClear, disabled,
    managedByEnvironment,
    onDraft, onToggleClear,
  } = props;
  const readOnly = disabled || managedByEnvironment;
  return (
    <div>
      <div className="flex flex-wrap items-center gap-2">
        <span className="text-sm font-medium text-ink">{label}</span>
        <span className={`text-xs ${configured && !pendingClear ? "text-ok" : "text-ink-3"}`}>
          {pendingClear ? l("将清除", "Will be removed") : configured ? l("已配置 ✓", "Configured ✓") : l("未配置", "Not configured")}
        </span>
        {managedByEnvironment && (
          <span className="text-xs text-warn">{l("启动环境托管（只读）", "Managed by startup environment (read-only)")}</span>
        )}
        {configured && !managedByEnvironment && (
          <button
            type="button"
            className="btn btn-sm ml-auto"
            aria-label={`${pendingClear ? l("撤销清除", "Undo removal") : l("清除", "Remove")}: ${label}`}
            onClick={onToggleClear}
            disabled={disabled}
          >
            {pendingClear ? l("撤销清除", "Undo removal") : l("清除", "Remove")}
          </button>
        )}
      </div>
      {hint && <p className="mt-0.5 text-xs text-ink-3">{hint}</p>}
      {managedByEnvironment && (
        <p className="mt-1 text-xs text-warn">
          {l("请完全停止 CareerDesk，在 shell / 容器启动环境中修改或移除该变量，再重新启动。", "Stop CareerDesk completely, change or remove the variable in the shell or container startup environment, then restart.")}
        </p>
      )}
      {!pendingClear && (
        <input
          className="input mt-1.5 w-full"
          type="password"
          aria-label={l(`${label}输入`, `${label} input`)}
          autoComplete="off"
          disabled={readOnly}
          value={draft}
          onChange={(e) => onDraft(e.target.value)}
          placeholder={configured
            ? configuredPlaceholder ?? l("输入新的 API Key（留空则不更改）", "Enter a new API key (leave blank to keep it)")
            : placeholder ?? l("输入 API Key", "Enter API key")}
        />
      )}
    </div>
  );
}

function StoredCredentialControl(props: {
  label: string;
  pendingClear: boolean;
  disabled: boolean;
  credentialStorageUnavailable: boolean;
  managedByEnvironment: boolean;
  onToggleClear: () => void;
}) {
  const l = useLocalizer();
  const {
    label,
    pendingClear,
    disabled,
    credentialStorageUnavailable,
    managedByEnvironment,
    onToggleClear,
  } = props;
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-xl bg-panel-2 p-3 text-xs text-ink-2">
      <span className="font-medium text-ink">{label}</span>
      <span className={pendingClear ? "text-warn" : "text-ok"}>
        {pendingClear ? l("保存时将清除", "Will be removed on save") : l("已保存，当前服务未启用", "Saved; current service is disabled")}
      </span>
      {managedByEnvironment ? (
        <span className="ml-auto text-warn">{l("启动环境托管，网页只读", "Managed by startup environment; read-only here")}</span>
      ) : (
        <button
          type="button"
          className="btn btn-sm ml-auto"
          aria-label={`${pendingClear ? l("撤销清除", "Undo removal") : l("清除", "Remove")}: ${label}`}
          disabled={disabled}
          onClick={onToggleClear}
        >
          {pendingClear ? l("撤销清除", "Undo removal") : l("清除", "Remove")}
        </button>
      )}
      {credentialStorageUnavailable && !managedByEnvironment && (
        <span className="w-full text-warn">{l("系统凭据存储当前不可用，暂时不能清除。", "System credential storage is unavailable, so this cannot be removed right now.")}</span>
      )}
    </div>
  );
}

function OutboundToggle(props: {
  id: string;
  title: string;
  description?: string;
  checked: boolean;
  disabled?: boolean;
  managedByEnvironment?: boolean;
  status: string;
  statusTone: "ok" | "warn" | "muted";
  onChange: (checked: boolean) => void;
}) {
  const l = useLocalizer();
  const {
    id, title, description, checked, disabled = false, managedByEnvironment = false,
    status, statusTone, onChange,
  } = props;
  const statusClass = statusTone === "ok"
    ? "text-ok"
    : statusTone === "warn"
      ? "text-warn"
      : "text-ink-3";
  const readOnly = disabled || managedByEnvironment;
  return (
    <div className={`flex items-start gap-4 rounded-xl border border-line p-3.5 ${readOnly ? "bg-panel-2" : "bg-panel"}`}>
      <div className="min-w-0 flex-1">
        <label htmlFor={id} className={`${readOnly ? "cursor-not-allowed" : "cursor-pointer"} text-sm font-medium text-ink`}>
          {title}
        </label>
        {description && (
          <p id={`${id}-description`} className="mt-0.5 text-xs leading-relaxed text-ink-3">
            {description}
          </p>
        )}
        <p id={`${id}-status`} className={`${description ? "mt-1" : "mt-0.5"} text-xs ${statusClass}`}>
          {status}
        </p>
        {managedByEnvironment && (
          <p className="mt-1 text-xs text-warn">{l("由启动环境托管，网页只读。", "Managed by the startup environment; read-only here.")}</p>
        )}
      </div>
      <input
        id={id}
        type="checkbox"
        role="switch"
        aria-describedby={description ? `${id}-description ${id}-status` : `${id}-status`}
        className="mt-0.5 h-5 w-5 shrink-0 cursor-pointer accent-[var(--accent)] disabled:cursor-not-allowed"
        checked={checked}
        disabled={readOnly}
        onChange={(event) => onChange(event.target.checked)}
      />
    </div>
  );
}

function CompatibleEndpointNotice(props: {
  endpoint: SettingsState["openai_compatible_endpoint"];
}) {
  const l = useLocalizer();
  const { endpoint } = props;
  const origin = endpoint.externally_managed ? l("启动环境", "startup environment") : ".env";
  if (endpoint.status === "configured") {
    return (
      <p className="rounded-xl bg-panel-2 p-3 text-xs leading-relaxed text-ink-2">
        {l(`当前服务地址（${endpoint.source}，来自${origin}）：`, `Current service URL (${endpoint.source}, from ${origin}):`)}{" "}
        <code className="break-all text-ink">{endpoint.url}</code>
      </p>
    );
  }
  if (endpoint.status === "invalid") {
    return (
      <p role="alert" className="rounded-xl bg-warn-soft p-3 text-xs leading-relaxed text-warn">
        {l(`${endpoint.source} 的服务地址无效（来自${origin}）。为避免泄露凭据，原值不会显示。`, `${endpoint.source} has an invalid service URL (from ${origin}). The original value is hidden to protect credentials.`)}
        {endpoint.issue ? ` ${endpoint.issue}.` : ""}
        {l("请完全停止 CareerDesk，修正后再重新启动。", " Stop CareerDesk completely, correct it, then restart.")}
      </p>
    );
  }
  return (
    <p className="rounded-xl bg-warn-soft p-3 text-xs leading-relaxed text-warn">
      {l("尚未配置服务地址。请完全停止 CareerDesk，在 .env 或启动环境中设置 OPENAI_BASE_URL（优先）或 LLM_BASE_URL，再重新启动。", "No service URL is configured. Stop CareerDesk completely, set OPENAI_BASE_URL (preferred) or LLM_BASE_URL in .env or the startup environment, then restart.")}
    </p>
  );
}

function ModelCapacityFields(props: {
  contextWindow: string;
  maxOutputTokens: string;
  disabled: boolean;
  managed: SettingsState["environment_managed"]["llm_capabilities"];
  onContextWindow: (value: string) => void;
  onMaxOutputTokens: (value: string) => void;
}) {
  const l = useLocalizer();
  const {
    contextWindow, maxOutputTokens, disabled, managed,
    onContextWindow, onMaxOutputTokens,
  } = props;
  return (
    <div className="grid gap-3 sm:grid-cols-2">
        <label className="text-xs text-ink-2">
          {l("一次最多可处理的内容量（Context window）", "Maximum input capacity (context window)")}
          <input
            aria-label={l("模型 context window", "Model context window")}
            className="input mt-1 w-full"
            inputMode="numeric"
            value={contextWindow}
            disabled={disabled || managed.context_window}
            onChange={(event) => onContextWindow(event.target.value)}
            placeholder={l("如 131072", "For example, 131072")}
          />
          {managed.context_window && <span className="mt-1 block text-warn">{l("由启动环境托管", "Managed by startup environment")}</span>}
        </label>
        <label className="text-xs text-ink-2">
          {l("一次最多可生成的内容量（Max output tokens）", "Maximum generation capacity (max output tokens)")}
          <input
            aria-label={l("模型 max output tokens", "Model max output tokens")}
            className="input mt-1 w-full"
            inputMode="numeric"
            value={maxOutputTokens}
            disabled={disabled || managed.max_output_tokens}
            onChange={(event) => onMaxOutputTokens(event.target.value)}
            placeholder={l("如 8192", "For example, 8192")}
          />
          {managed.max_output_tokens && <span className="mt-1 block text-warn">{l("由启动环境托管", "Managed by startup environment")}</span>}
        </label>
    </div>
  );
}

function ModelAndPrivacySettingsSection() {
  const l = useLocalizer();
  const { locale } = useLocale();
  const [state, setState] = useState<SettingsState | null>(null);
  const [loadError, setLoadError] = useState(false);
  // Provider choice is none, custom, or a known provider; modelDraft belongs to the selected provider.
  const [choice, setChoice] = useState<string>("none");
  const [modelDraft, setModelDraft] = useState("");
  const [custom, setCustom] = useState("");
  const [contextWindowDraft, setContextWindowDraft] = useState("");
  const [maxOutputTokensDraft, setMaxOutputTokensDraft] = useState("");
  // Credential drafts record only explicit replacements or removals and send only those on save.
  const [keyDrafts, setKeyDrafts] = useState(EMPTY_CREDENTIAL_DRAFTS);
  const [clears, setClears] = useState(EMPTY_CREDENTIAL_CLEARS);
  const [policyDraft, setPolicyDraft] = useState<OutboundPolicy>({ ...DEFAULT_OUTBOUND_POLICY });
  const [busy, setBusy] = useState(false);
  const [activeSave, setActiveSave] = useState<SettingsSaveScope | null>(null);
  const [notice, setNotice] = useState<{
    scope: SettingsSaveScope;
    kind: "ok" | "bad";
    text: string;
  } | null>(null);

  useEffect(() => {
    getJson<SettingsState>("/api/settings")
      .then((s) => {
        setState(s);
        setPolicyDraft(s.outbound_policy);
        setContextWindowDraft(s.llm_capabilities.context_window?.toString() ?? "");
        setMaxOutputTokensDraft(s.llm_capabilities.max_output_tokens?.toString() ?? "");
        // Map a saved provider or provider:model value into the selector; unknown providers remain custom.
        const value = s.llm_model;
        if (value === null) {
          setChoice("none");
          return;
        }
        const colon = value.indexOf(":");
        const provider = colon === -1 ? value : value.slice(0, colon);
        if (s.providers.some((p) => p.name === provider)) {
          setChoice(provider);
          setModelDraft(colon === -1 ? "" : value.slice(colon + 1));
        } else {
          setChoice("custom");
          setCustom(value);
        }
      })
      .catch(() => setLoadError(true));
  }, []);

  if (loadError) return <p className="text-sm text-ink-3">{l("暂时无法读取设置。请确认 CareerDesk 正常运行后刷新页面。", "Settings could not be loaded. Confirm CareerDesk is running, then refresh.")}</p>;
  if (!state) return null;

  if (!state.editable) {
    const policy = state.outbound_policy;
    return (
      <div className="card max-w-2xl p-5 text-sm text-ink-2">
        <p>{l("此版本的模型、凭据与联网策略由管理员统一配置，无法在这里修改。", "An administrator manages the model, credentials, and network policy for this deployment; they cannot be changed here.")}</p>
        <p className="mt-2">
          {l("当前模型：", "Current model: ")}{state.llm_model
            ? <code className="text-ink">{state.llm_model}</code>
            : <span className="text-ink-3">{l("未配置", "Not configured")}</span>}
        </p>
        {state.llm_model && state.llm_capabilities.source !== "provider" && (
          <p className="mt-2 text-xs text-ink-3">
            {l("自定义模型容量：", "Custom model capacity: ")}{state.llm_capabilities.source === "missing"
              ? <span className="text-warn">{l("尚未填写，AI 功能会暂停以避免请求失败", "Missing; AI features are paused to prevent failed requests")}</span>
              : l(`一次最多处理 ${state.llm_capabilities.context_window ?? "—"} tokens；一次最多生成 ${state.llm_capabilities.max_output_tokens ?? "—"} tokens`, `Up to ${state.llm_capabilities.context_window ?? "—"} input tokens and ${state.llm_capabilities.max_output_tokens ?? "—"} output tokens`)}
          </p>
        )}
        {state.llm_model?.split(":", 1)[0] === "openai_compatible" && (
          <div className="mt-3">
            <CompatibleEndpointNotice endpoint={state.openai_compatible_endpoint} />
          </div>
        )}
        <div className="mt-4 border-t border-line pt-4">
          <p className="font-medium text-ink">
            {l("严格离线：", "Strict offline: ")}{policy.strict_offline ? l("已开启", "On") : l("未开启", "Off")}
          </p>
          <ul className="mt-2 space-y-1 text-xs text-ink-3">
            <li>{l("跨会话语义搜索：", "Cross-session semantic search: ")}{policy.allow_conversation_embedding ? l("已允许", "Allowed") : l("未授权", "Not allowed")}</li>
            <li>{l("联网公司调研：", "Online company research: ")}{policy.allow_web_research ? l("已允许", "Allowed") : l("未授权", "Not allowed")}</li>
          </ul>
          {policy.strict_offline && (
            <p className="mt-2 text-xs text-warn">{l("严格离线当前覆盖并暂停以上全部出网授权。", "Strict offline currently overrides and pauses every network permission above.")}</p>
          )}
        </div>
      </div>
    );
  }

  const providers = state.providers;
  const providerLabel = (provider: ProviderInfo) => modelProviderLabel(provider, locale);
  const cloud = providers.filter((p) => !p.local && p.default_model);
  const local = providers.filter((p) => p.local);
  const advanced = providers.filter((p) => !p.local && !p.default_model);
  const selected = providers.find((p) => p.name === choice) ?? null;
  const selectedDraftUsesTrustedDefault = Boolean(
    selected?.default_model
    && selected.context_window
    && selected.max_output_tokens
    && (!modelDraft.trim() || modelDraft.trim() === selected.default_model),
  );
  const currentConfiguredOverride = state.llm_capabilities.source === "configured";
  const selectedModelValue = choice === "custom"
    ? custom.trim()
    : selected
      ? (modelDraft.trim() ? `${selected.name}:${modelDraft.trim()}` : selected.name)
      : null;
  const showCustomCapacity = choice !== "none"
    && (!selectedDraftUsesTrustedDefault
      || (currentConfiguredOverride && selectedModelValue === state.llm_model));

  const keyFieldProps = (scope: SettingsSaveScope, name: string) => ({
    configured: state.keys[name] ?? false,
    draft: keyDrafts[scope][name] ?? "",
    pendingClear: clears[scope][name] ?? false,
    disabled: busy || !state.credential_storage.available,
    managedByEnvironment: state.environment_managed.keys[name] ?? false,
    onDraft: (value: string) => setKeyDrafts((current) => ({
      ...current,
      [scope]: { ...current[scope], [name]: value },
    })),
    onToggleClear: () => setClears((current) => ({
      ...current,
      [scope]: { ...current[scope], [name]: !current[scope][name] },
    })),
  });
  const discardKeyDraft = (scope: SettingsSaveScope, name: string) => setKeyDrafts((current) => {
    const next = { ...current[scope] };
    delete next[name];
    return { ...current, [scope]: next };
  });

  const keyAvailable = (scope: SettingsSaveScope, name: string) => !clears[scope][name]
    && (Boolean(keyDrafts[scope][name]?.trim()) || Boolean(state.keys[name]));
  const outboundKeys = {
    openai: keyAvailable("retrieval", "OPENAI_API_KEY"),
    tavily: keyAvailable("research", "TAVILY_API_KEY"),
    brave: keyAvailable("research", "BRAVE_API_KEY"),
    google: keyAvailable("research", "GOOGLE_PSE_API_KEY")
      && keyAvailable("research", "GOOGLE_PSE_ENGINE_ID"),
    searxng: keyAvailable("research", "SEARXNG_BASE_URL"),
  };
  const policyForScope = (scope: SettingsSaveScope): OutboundPolicy => {
    const policy = { ...state.outbound_policy };
    for (const field of POLICY_FIELDS_BY_SCOPE[scope]) policy[field] = policyDraft[field];
    return policy;
  };
  const capabilityStatus = (permission: OutboundPermission) => outboundCapabilityStatus(
    permission === "allow_conversation_embedding"
      ? policyForScope("retrieval")
      : policyForScope("research"),
    permission,
    outboundKeys,
  );
  const capabilityTone = (permission: OutboundPermission): "ok" | "warn" | "muted" => {
    const status = capabilityStatus(permission);
    if (status === "disabled") return "muted";
    if (status === "paused_by_strict_offline" || status === "missing_openai_key"
      || status === "no_search_outlet") return "warn";
    return "ok";
  };
  const updatePermission = (permission: OutboundPermission, enabled: boolean) => {
    setPolicyDraft((policy) => ({ ...policy, [permission]: enabled }));
  };
  const updatePolicyFlag = (field: "allow_deep_research" | "allow_ddg_fallback",
                            enabled: boolean) => {
    setPolicyDraft((policy) => ({ ...policy, [field]: enabled }));
  };
  const grantedPermissionCount = enabledOutboundPermissionCount(state.outbound_policy);
  const vectorEnabled = policyDraft.allow_conversation_embedding;
  const savedProviderName = state.llm_model?.split(":", 1)[0] ?? null;
  const savedProvider = providers.find((provider) => provider.name === savedProviderName) ?? null;
  const llmUsesOpenAIKey = Boolean(savedProvider?.key_vars.includes("OPENAI_API_KEY"));
  const changeModelChoice = (nextChoice: string) => {
    const nextProvider = providers.find((provider) => provider.name === nextChoice);
    const visibleKeyNames = new Set(nextProvider?.key_vars ?? []);
    setKeyDrafts((current) => ({
      ...current,
      model: retainVisibleCredentialChanges(current.model, visibleKeyNames),
    }));

    // A stored unused OpenAI/search key keeps an explicit clear control below,
    // so its pending deletion remains visible. Provider-only credentials have
    // no such inactive-service row: cancel their pending clear when switching
    // away instead of silently submitting a now-hidden destructive change.
    setClears((current) => ({
      ...current,
      model: retainVisibleCredentialChanges(current.model, visibleKeyNames),
    }));
    setChoice(nextChoice);
  };
  const resumingStoredPermissions = state.outbound_policy.strict_offline
    && !policyDraft.strict_offline
    && grantedPermissionCount > 0;
  const strictModelBlocked = state.outbound_policy.strict_offline
    && choice !== "none"
    && selected?.local !== true;
  const webResearchActive = outboundPermissionIsEffective(
    state.outbound_policy,
    "allow_web_research",
  );
  const anyEnvironmentManaged = state.environment_managed.llm_model
    || Object.values(state.environment_managed.llm_capabilities).some(Boolean)
    || Object.values(state.environment_managed.keys).some(Boolean)
    || Object.values(state.environment_managed.outbound_policy).some(Boolean);

  async function save(scope: SettingsSaveScope) {
    const currentState = state;
    if (currentState === null) return;
    const body: SettingsUpdate = { revision: currentState.revision };

    if (scope === "model") {
      let model: string | null;
      if (choice === "none") {
        model = null;
      } else if (choice === "custom") {
        if (!custom.trim()) {
          setNotice({
            scope,
            kind: "bad",
            text: l("请填写自定义连接，格式为“服务:模型名称”，例如 openai_compatible:my-model。", "Enter a custom connection as provider:model, for example openai_compatible:my-model."),
          });
          return;
        }
        model = custom.trim();
      } else {
        const provider = selected!;
        const modelName = modelDraft.trim();
        if (!modelName && !provider.default_model) {
          setNotice({
            scope,
            kind: "bad",
            text: l(`${providerLabel(provider)} 没有预设模型，请填写服务中显示的模型名称。`, `${providerLabel(provider)} has no preset model. Enter the model name shown by the service.`),
          });
          return;
        }
        model = modelName ? `${provider.name}:${modelName}` : provider.name;
      }

      const chosenProvider = model ? model.split(":", 1)[0] : null;
      const modelChanged = model !== (currentState.llm_model ?? null);
      const retainedConfiguredOverride = !modelChanged && currentConfiguredOverride;
      const needsExplicitCapabilities = Boolean(model)
        && (!selectedDraftUsesTrustedDefault || retainedConfiguredOverride);
      let capabilities: ModelCapabilities = {
        context_window: null,
        max_output_tokens: null,
      };
      if (needsExplicitCapabilities) {
        const contextText = contextWindowDraft.trim();
        const outputText = maxOutputTokensDraft.trim();
        if (!/^\d+$/.test(contextText) || !/^\d+$/.test(outputText)) {
          setNotice({
            scope,
            kind: "bad",
            text: l("这个模型没有预设容量。请在“自定义模型容量”中填写两个不带小数的数字。", "This model has no preset capacity. Enter two whole numbers under Custom model capacity."),
          });
          return;
        }
        const contextWindow = Number(contextText);
        const maxOutputTokens = Number(outputText);
        if (
          !Number.isSafeInteger(contextWindow)
          || !Number.isSafeInteger(maxOutputTokens)
          || contextWindow < 1024
          || maxOutputTokens < 256
          || maxOutputTokens > contextWindow
        ) {
          setNotice({
            scope,
            kind: "bad",
            text: l("容量设置无效：一次处理量至少为 1024，一次生成量至少为 256，而且生成量不能大于处理量。", "Invalid capacity: input capacity must be at least 1024, output capacity at least 256, and output cannot exceed input."),
          });
          return;
        }
        capabilities = {
          context_window: contextWindow,
          max_output_tokens: maxOutputTokens,
        };
      }

      // Each card saves independently; model validation reads the saved network switch only.
      const credentialOptions = cloudModelCredentialOptions(currentState.outbound_policy, selected);
      if (credentialOptions.length > 0
          && !credentialOptions.some((name) => keyAvailable("model", name))) {
        setNotice({
          scope,
          kind: "bad",
          text: l(`${providerLabel(selected!)} 需要配置 ${credentialOptions.join(" / ")} 中的任意一项；请和模型一起保存。`, `${providerLabel(selected!)} requires one of ${credentialOptions.join(" / ")}. Save the credential with the model.`),
        });
        return;
      }
      const endpointError = openAICompatibleEndpointSaveError(
        currentState.outbound_policy.strict_offline,
        chosenProvider,
        currentState.openai_compatible_endpoint.status,
        locale,
      );
      if (endpointError) {
        setNotice({ scope, kind: "bad", text: endpointError });
        return;
      }

      if (modelChanged) body.llm_model = model;
      const capabilitiesChanged = needsExplicitCapabilities
        ? currentState.llm_capabilities.source !== "configured"
          || capabilities.context_window !== currentState.llm_capabilities.context_window
          || capabilities.max_output_tokens !== currentState.llm_capabilities.max_output_tokens
        : currentState.llm_capabilities.source === "configured";
      if (modelChanged || capabilitiesChanged) body.llm_capabilities = capabilities;
    } else {
      const nextPolicy = policyForScope(scope);
      if (POLICY_FIELDS_BY_SCOPE[scope].some(
        (field) => nextPolicy[field] !== currentState.outbound_policy[field],
      )) {
        body.outbound_policy = nextPolicy;
      }
    }

    const keys: Record<string, string | null> = {};
    for (const [name, pending] of Object.entries(clears[scope])) {
      if (pending) keys[name] = null;
    }
    for (const [name, draft] of Object.entries(keyDrafts[scope])) {
      if (draft.trim() && !clears[scope][name]) keys[name] = draft.trim();
    }
    if (Object.keys(keys).length) body.keys = keys;

    if (!("llm_model" in body) && !("llm_capabilities" in body) && !body.keys && !body.outbound_policy) {
      setNotice({ scope, kind: "ok", text: l("没有要保存的改动。", "No changes to save.") });
      return;
    }
    setBusy(true);
    setActiveSave(scope);
    setNotice(null);
    try {
      const next = await putJson<SettingsState>("/api/settings", body);
      // CSP is a document-level response policy; an API response cannot retroactively alter the current document.
      if (scope === "privacy"
          && strictOfflineChangeRequiresReload(currentState.outbound_policy, next.outbound_policy)) {
        window.location.reload();
        return;
      }
      setState(next);
      setPolicyDraft((current) => {
        const merged = { ...current };
        for (const field of POLICY_FIELDS_BY_SCOPE[scope]) {
          merged[field] = next.outbound_policy[field];
        }
        return merged;
      });
      if (scope === "model") {
        setContextWindowDraft(next.llm_capabilities.context_window?.toString() ?? "");
        setMaxOutputTokensDraft(next.llm_capabilities.max_output_tokens?.toString() ?? "");
        if (choice === "custom") setCustom(next.llm_model ?? "");
      }
      setKeyDrafts((current) => ({ ...current, [scope]: {} }));
      setClears((current) => ({ ...current, [scope]: {} }));
      setNotice({ scope, kind: "ok", text: l("已保存，立即生效。", "Saved and effective immediately.") });
    } catch (e) {
      setNotice({
        scope,
        kind: "bad",
        text: e instanceof HttpError && e.status === 409
          ? l("设置已在另一个窗口中更新，本次未保存。刷新会清空当前草稿：请先复制不含凭据的模型和策略选择；API Key 刷新后需要重新输入。", "Settings changed in another window, so this save was rejected. Refreshing clears this draft: first copy non-secret model and policy choices; API keys must be re-entered afterward.")
          : e instanceof Error ? e.message : l("保存失败", "Save failed"),
      });
    } finally {
      setBusy(false);
      setActiveSave(null);
    }
  }

  async function clearConversationHistory() {
    if (!window.confirm(l("彻底删除你的全部历史对话及本机检索索引？此操作无法撤销。", "Permanently delete all conversation history and the local search index? This cannot be undone."))) return;
    setBusy(true);
    setNotice(null);
    try {
      await postJson<{ status: "completed"; deleted_messages: number }>(
        "/api/settings/conversation-history/clear",
      );
    } catch (error) {
      setNotice({
        scope: "retrieval",
        kind: "bad",
        text: error instanceof Error ? error.message : l("历史对话删除失败", "Could not delete conversation history"),
      });
    } finally {
      setBusy(false);
    }
  }

  const saveControls = (scope: SettingsSaveScope) => (
    <div className="mt-5 flex flex-wrap items-center gap-3 border-t border-line pt-4">
      <button type="button" className="btn-primary" onClick={() => void save(scope)} disabled={busy}>
        {busy && activeSave === scope ? l("正在保存…", "Saving…") : l("保存设置", "Save settings")}
      </button>
      {notice?.scope === scope && (
        <span
          role={notice.kind === "ok" ? "status" : "alert"}
          className={`text-sm ${notice.kind === "ok" ? "text-ok" : "text-bad"}`}
        >
          {notice.text}
        </span>
      )}
    </div>
  );

  return (
    <div className="flex min-w-0 flex-col gap-4">
      {state.persistence_warning && (
        <p role="alert" className="rounded-xl bg-warn-soft px-4 py-3 text-sm text-warn">
          {state.persistence_warning}
        </p>
      )}
      {!state.credential_storage.available && (
        <p role="alert" className="rounded-xl bg-warn-soft px-4 py-3 text-sm leading-relaxed text-warn">
          {state.credential_storage.issue ?? l("系统凭据存储当前不可用。", "System credential storage is unavailable.")}
          {l("凭据输入已停用；无需凭据的本地功能和其他非凭据设置仍可使用。", " Credential input is disabled; local features that need no credentials and other non-secret settings remain available.")}
        </p>
      )}
      {anyEnvironmentManaged && (
        <p role="status" className="rounded-xl bg-warn-soft px-4 py-3 text-sm leading-relaxed text-warn">
          {l("部分设置由运行环境管理，已在对应位置标为只读；其他设置仍可正常修改和保存。", "Some settings are managed by the runtime environment and marked read-only. Other settings can still be changed and saved.")}
        </p>
      )}
      <section id="settings-privacy" className="card scroll-mt-48 p-5 min-[560px]:scroll-mt-36 md:scroll-mt-28">
        <h2 className="text-sm font-semibold">{l("联网设置", "Network access")}</h2>
        <p className="mt-1 text-xs leading-relaxed text-ink-3">
          {l("一键暂停云端模型、向量模型和联网调研等外部服务，已保存的选择和 API Key 不会被删除。", "Pause cloud models, embedding models, online research, and other external services without deleting saved choices or API keys.")}
        </p>
        <div className="mt-4 flex flex-col gap-3">
          <OutboundToggle
            id="strict-offline"
            title={l("暂停所有联网功能", "Pause all network features")}
            checked={policyDraft.strict_offline}
            disabled={busy}
            managedByEnvironment={state.environment_managed.outbound_policy.strict_offline}
            status={policyDraft.strict_offline
              ? l(`当前已暂停；保留了 ${grantedPermissionCount} 项联网选择`, `Paused; ${grantedPermissionCount} saved network choices are retained`)
              : l("当前未暂停；是否联网仍取决于下方各项设置", "Not paused; network access still depends on each setting below")}
            statusTone={policyDraft.strict_offline ? "ok" : "muted"}
            onChange={(checked) => setPolicyDraft((policy) => ({
              ...policy,
              strict_offline: checked,
            }))}
          />
          {resumingStoredPermissions && (
            <p role="status" className="rounded-xl bg-warn-soft px-3 py-2 text-xs text-warn">
              {l(`保存后会恢复 ${grantedPermissionCount} 项联网功能。请先确认下方设置。`, `Saving will resume ${grantedPermissionCount} network features. Review the settings below first.`)}
            </p>
          )}
        </div>
        {saveControls("privacy")}
      </section>
      {/* Provider and model */}
      <section id="settings-model" className="card scroll-mt-48 p-5 min-[560px]:scroll-mt-36 md:scroll-mt-28">
        <h2 className="text-sm font-semibold">{l("AI 模型", "AI model")}</h2>
        <p className="mt-1 text-xs leading-relaxed text-ink-3">
          {l("选择求职助手使用的模型。", "Choose the model used by the career assistant.")}
        </p>
        {state.environment_managed.llm_model && (
          <p className="mt-3 rounded-xl bg-warn-soft p-3 text-xs leading-relaxed text-warn">
            {l("APP_LLM_MODEL 由启动环境托管，模型选择只读。请完全停止 CareerDesk，在启动环境中修改或移除该变量，再重新启动。", "APP_LLM_MODEL is managed by the startup environment, so model selection is read-only. Stop CareerDesk completely, change or remove the variable there, then restart.")}
          </p>
        )}
        {Object.values(state.environment_managed.llm_capabilities).some(Boolean) && (
          <p className="mt-3 rounded-xl bg-warn-soft p-3 text-xs leading-relaxed text-warn">
            {l("模型容量由启动环境托管。容量与型号必须保持匹配；如需换型号，请完全停止 CareerDesk，在启动环境中一并修改 APP_LLM_MODEL、APP_LLM_CONTEXT_WINDOW 与 APP_LLM_MAX_OUTPUT_TOKENS。", "Model capacity is managed by the startup environment and must match the model. To change models, stop CareerDesk completely and update APP_LLM_MODEL, APP_LLM_CONTEXT_WINDOW, and APP_LLM_MAX_OUTPUT_TOKENS together.")}
          </p>
        )}
        <label className="mt-4 block text-xs font-medium text-ink-2">
          {l("模型服务", "Model service")}
          <select
            aria-label={l("选择模型厂商", "Choose model provider")}
            className="input mt-1.5 w-full"
            value={choice}
            disabled={busy || state.environment_managed.llm_model
              || Object.values(state.environment_managed.llm_capabilities).some(Boolean)}
            onChange={(e) => {
              changeModelChoice(e.target.value);
              setModelDraft("");
              setContextWindowDraft("");
              setMaxOutputTokensDraft("");
            }}
          >
            <optgroup label={l("云端服务（任务内容会发送到服务商）", "Cloud services (task content is sent to the provider)")}>
              {cloud.map((p) => (
                <option key={p.name} value={p.name}>
                  {providerLabel(p)}
                </option>
              ))}
            </optgroup>
            <optgroup label={l("本地模型（模型推理留在本机）", "Local models (inference stays on this device)")}>
              {local.map((p) => (
                <option key={p.name} value={p.name}>
                  {providerLabel(p)}
                </option>
              ))}
            </optgroup>
            <optgroup label={l("其他兼容服务（高级设置）", "Other compatible services (advanced)")}>
              {advanced.map((p) => (
                <option key={p.name} value={p.name}>
                  {providerLabel(p)}
                </option>
              ))}
            </optgroup>
            <optgroup label={l("自定义 / 不配置", "Custom / not configured")}>
              <option value="custom">{l("自定义连接（填写 服务:型号）", "Custom connection (enter provider:model)")}</option>
              <option value="none">{l("暂不配置", "Not configured")}</option>
            </optgroup>
          </select>
        </label>

        {selected && (
          <div className="mt-4 flex flex-col gap-4">
            <p className={`rounded-xl p-3 text-xs ${selected.local && !strictModelBlocked ? "bg-ok-soft text-ok" : "bg-panel-2 text-ink-2"}`}>
              {strictModelBlocked
                ? l("当前模型配置会保留，但严格离线期间不会调用。要继续使用 AI 功能，请改用明确标记为本地的模型，或关闭严格离线。", "The model configuration is retained but cannot be called in strict offline mode. To use AI features, choose a model explicitly marked as local or turn off strict offline.")
                : selected.local
                  ? l(`模型在本机运行；联网公司调研当前${webResearchActive ? "已单独启用" : "未启用"}。`, `The model runs locally; online company research is ${webResearchActive ? "enabled separately" : "off"}.`)
                  : (
                    <>
                      {l("使用云端模型时，完成任务所需的文字，包括简历全文等内容，会发送给所选模型的服务商。", "When using a cloud model, text needed for the task—including full résumé content—is sent to the selected provider.")}
                      <br />
                      {l("原始文件和 CareerDesk 数据库仍留在本机；服务商如何处理收到的内容，以你与该服务商之间的设置和条款为准。", "Original files and the CareerDesk database remain on this device. The provider handles received content according to your settings and terms with that provider.")}
                    </>
                  )}
            </p>
            {selected.name === "openai_compatible" && (
              <CompatibleEndpointNotice endpoint={state.openai_compatible_endpoint} />
            )}
            <div>
              <span className="text-sm font-medium text-ink">{l("模型名称", "Model name")}</span>
              <input
                className="input mt-1.5 w-full"
                aria-label={l(`${providerLabel(selected)} 型号`, `${providerLabel(selected)} model`)}
                value={modelDraft}
                disabled={busy || state.environment_managed.llm_model
                  || Object.values(state.environment_managed.llm_capabilities).some(Boolean)}
                onChange={(e) => {
                  setModelDraft(e.target.value);
                  setContextWindowDraft("");
                  setMaxOutputTokensDraft("");
                }}
                placeholder={
                  selected.default_model
                    ? l(`留空即可使用默认模型：${selected.default_model}`, `Leave blank to use the default: ${selected.default_model}`)
                    : selected.name === "ollama"
                      ? l("必填，如 qwen3", "Required, for example qwen3")
                      : l("必填：填写服务中显示的模型名称", "Required: enter the model name shown by the service")
                }
              />
            </div>
            {selected.local && (
              <p className="text-xs text-ink-3">
                {selected.name === "ollama" ? l(LOCAL_GUIDES_ZH.ollama, LOCAL_GUIDES_EN.ollama)
                  : selected.name === "vllm" ? l(LOCAL_GUIDES_ZH.vllm, LOCAL_GUIDES_EN.vllm)
                    : selected.name === "sglang" ? l(LOCAL_GUIDES_ZH.sglang, LOCAL_GUIDES_EN.sglang)
                      : l("本地推理服务无需 API Key；其他联网能力仍需单独启用。", "Local inference services need no API key; other network capabilities must still be enabled separately.")}
              </p>
            )}
            {selected.key_vars.map((keyVar, index) => (
              <KeyField
                key={keyVar}
                label={l(`${providerLabel(selected)} API Key（${keyVar}）`, `${providerLabel(selected)} API key (${keyVar})`)}
                hint={selected.key_vars.length === 1
                  ? undefined
                  : index === 0
                    ? l("调用该厂商的首选凭据；下方其他凭据也可作为备选", "Preferred credential for this provider; other credentials below can be fallbacks")
                  : l(`备选凭据；${selected.key_vars.join(" / ")} 任意配置一项即可`, `Fallback credential; configure any one of ${selected.key_vars.join(" / ")}`)}
                {...keyFieldProps("model", keyVar)}
              />
            ))}
          </div>
        )}
        {choice === "custom" && (
          <div className="mt-4">
            <p className="mb-3 rounded-xl bg-warn-soft p-3 text-xs text-ink-2">
              {l("自定义连接适合熟悉服务地址和模型名称的用户。CareerDesk 无法核实该服务的运营方、安全性或数据处理方式；发送内容前请自行确认地址和服务条款。", "Custom connections are for users familiar with service URLs and model names. CareerDesk cannot verify the operator, security, or data handling; confirm the address and terms before sending content.")}
            </p>
            {strictModelBlocked && (
              <p role="status" className="mb-3 rounded-xl bg-warn-soft p-3 text-xs text-warn">
                {l("严格离线会把自定义兼容通道按远端服务保守阻止；配置会保留，但不会调用。", "Strict offline conservatively treats custom compatible endpoints as remote. The configuration remains, but is not called.")}
              </p>
            )}
            <input
              aria-label={l("自定义模型串", "Custom model identifier")}
              className="input w-full"
              value={custom}
              disabled={busy || state.environment_managed.llm_model
                || Object.values(state.environment_managed.llm_capabilities).some(Boolean)}
              onChange={(e) => {
                setCustom(e.target.value);
                setContextWindowDraft("");
                setMaxOutputTokensDraft("");
              }}
              placeholder={l("服务:模型名称", "provider:model")}
              autoFocus
            />
          </div>
        )}
        {choice === "none" && (
          <p role="status" className="mt-4 rounded-xl bg-warn-soft p-3 text-xs text-ink-2">
            {l("暂不配置时仍可浏览和管理本地记录；求职助手、资料解析、公司调研和回答评估将不可用。", "Without a model, you can still browse and manage local records, but the career assistant, document parsing, company research, and answer evaluation are unavailable.")}
          </p>
        )}
        {showCustomCapacity && (
          <div className="mt-5 border-t border-line pt-4">
            <h3 className="text-sm font-medium text-ink">{l("自定义模型容量", "Custom model capacity")}</h3>
            <p className="mt-1 text-xs leading-relaxed text-ink-3">
              {l("只有自填模型才需要设置。请照抄服务商文档中的数值；填得过大可能导致请求失败，填得过小会限制可处理的内容。", "Only custom models need this. Copy the provider's documented values: values that are too high may fail requests, while low values limit usable content.")}
            </p>
            <div className="mt-3">
              <ModelCapacityFields
                contextWindow={contextWindowDraft}
                maxOutputTokens={maxOutputTokensDraft}
                disabled={busy}
                managed={state.environment_managed.llm_capabilities}
                onContextWindow={setContextWindowDraft}
                onMaxOutputTokens={setMaxOutputTokensDraft}
              />
            </div>
          </div>
        )}
        {saveControls("model")}
      </section>

      <section id="settings-retrieval" className="card scroll-mt-48 p-5 min-[560px]:scroll-mt-36 md:scroll-mt-28">
        <h2 className="text-sm font-semibold">{l("向量模型", "Embedding model")}</h2>
        <p className="mt-1 text-xs leading-relaxed text-ink-3">
          {l("可选的增强功能，用于加强求职助手的搜索能力等。如需适配其他向量模型，欢迎通过 GitHub Issue 提交需求。", "An optional enhancement for stronger assistant search. Request support for other embedding models through a GitHub issue.")}
        </p>
        <div className="mt-4 flex flex-col gap-3">
          <OutboundToggle
            id="allow-conversation-embedding"
            title={l("使用向量模型增强搜索", "Use embeddings to improve search")}
            description={l("开启后，聊天片段会发送给 OpenAI 的 text-embedding-3-small 模型生成检索索引。对话和生成的检索索引均保存在本机。", "When enabled, chat excerpts are sent to OpenAI's text-embedding-3-small model to build a search index. Conversations and the generated index remain on this device.")}
            checked={policyDraft.allow_conversation_embedding}
            disabled={busy}
            managedByEnvironment={state.environment_managed.outbound_policy.allow_conversation_embedding}
            status={outboundCapabilityStatusText(capabilityStatus("allow_conversation_embedding"), locale)}
            statusTone={capabilityTone("allow_conversation_embedding")}
            onChange={(checked) => {
              updatePermission("allow_conversation_embedding", checked);
              if (!checked && !llmUsesOpenAIKey) {
                discardKeyDraft("retrieval", "OPENAI_API_KEY");
              }
            }}
          />
          {vectorEnabled && (llmUsesOpenAIKey ? (
            <p className="rounded-xl bg-panel-2 p-3 text-xs leading-relaxed text-ink-2">
              {l("向量模型会共用 AI 模型中的", "The embedding model shares the AI model's")} <code>OPENAI_API_KEY</code>{l("；", ";")}
              {l("当前", " it is currently ")}{keyAvailable("retrieval", "OPENAI_API_KEY") ? l("已提供", "available") : l("尚未提供", "missing")}.
              {l("聊天片段会直接发送给 OpenAI，不会发送到自定义模型地址。", " Chat excerpts are sent directly to OpenAI, not to a custom model URL.")}
            </p>
          ) : (
            <KeyField
              label="OpenAI API Key（OPENAI_API_KEY）"
              {...keyFieldProps("retrieval", "OPENAI_API_KEY")}
            />
          ))}
          {!vectorEnabled && !llmUsesOpenAIKey && state.keys.OPENAI_API_KEY && (
            <StoredCredentialControl
              label={l("OpenAI 向量模型 API Key", "OpenAI embedding API key")}
              pendingClear={Boolean(clears.retrieval.OPENAI_API_KEY)}
              disabled={busy || !state.credential_storage.available}
              credentialStorageUnavailable={!state.credential_storage.available}
              managedByEnvironment={state.environment_managed.keys.OPENAI_API_KEY ?? false}
              onToggleClear={() => setClears((current) => ({
                ...current,
                retrieval: {
                  ...current.retrieval,
                  OPENAI_API_KEY: !current.retrieval.OPENAI_API_KEY,
                },
              }))}
            />
          )}
        </div>
        <div className="mt-4 border-t border-line pt-4">
          <button className="btn btn-danger" type="button" disabled={busy} onClick={clearConversationHistory}>
            {l("删除全部历史对话", "Delete all conversation history")}
          </button>
          <p className="mt-2 text-xs text-ink-3">{l("同时删除对话和检索索引，不影响其他求职资料或长期偏好。", "Deletes conversations and the search index without affecting other career data or long-term preferences.")}</p>
        </div>
        {saveControls("retrieval")}
      </section>

      <section id="settings-research" className="card scroll-mt-48 p-5 min-[560px]:scroll-mt-36 md:scroll-mt-28" aria-labelledby="web-research-settings-title">
        <h2 id="web-research-settings-title" className="text-sm font-semibold">{l("调研 API", "Research APIs")}</h2>
        <p className="mt-1 text-xs leading-relaxed text-ink-3">
          {l("用联网搜索服务查找公司和岗位的公开网页。添加的服务越多，搜集到的信息越全面。", "Use online search services to find public pages about companies and roles. More configured services can provide broader coverage.")}
          {policyDraft.allow_web_research && (
            <>
              <br />
              {l("当前已开启服务：", "Currently enabled services: ")}
              {configuredSearchOutletNames(outboundKeys, policyDraft.allow_ddg_fallback)
                .join(l("、", ", ")) || l("无", "None")}
            </>
          )}
        </p>
        <div className="mt-4 flex flex-col gap-3">
          <OutboundToggle
            id="allow-web-research"
            title={l("允许联网调研", "Allow online research")}
            checked={policyDraft.allow_web_research}
            disabled={busy}
            managedByEnvironment={state.environment_managed.outbound_policy.allow_web_research}
            status={outboundCapabilityStatusText(capabilityStatus("allow_web_research"), locale)}
            statusTone={capabilityTone("allow_web_research")}
            onChange={(checked) => {
              updatePermission("allow_web_research", checked);
              if (!checked) {
                for (const name of SEARCH_CREDENTIAL_NAMES) discardKeyDraft("research", name);
              }
            }}
          />
          {policyDraft.allow_web_research && (
            <>
              <KeyField
                label="Tavily API Key（TAVILY_API_KEY）"
                hint={l("在 Tavily 网站自行申请。费用、配额和数据规则请以其当前页面为准。", "Apply on Tavily's website. Refer to its current pages for pricing, quotas, and data practices.")}
                {...keyFieldProps("research", "TAVILY_API_KEY")}
              />
              <KeyField
                label="Brave Search API Key（BRAVE_API_KEY）"
                hint={l("在 Brave Search API 网站自行申请。费用、配额和数据规则请以其当前页面为准。", "Apply on the Brave Search API website. Refer to its current pages for pricing, quotas, and data practices.")}
                {...keyFieldProps("research", "BRAVE_API_KEY")}
              />
              <KeyField
                label="Google Programmable Search API Key（GOOGLE_PSE_API_KEY）"
                hint={l("在 Google Programmable Search 中自行申请，并与下方搜索引擎 ID 配套使用。费用、配额和数据规则请以其当前页面为准。", "Apply through Google Programmable Search and use it with the search engine ID below. Refer to Google's current pages for pricing, quotas, and data practices.")}
                {...keyFieldProps("research", "GOOGLE_PSE_API_KEY")}
              />
              <KeyField
                label={l("Google 搜索引擎 ID（GOOGLE_PSE_ENGINE_ID）", "Google search engine ID (GOOGLE_PSE_ENGINE_ID)")}
                hint={l("创建 Programmable Search Engine 后取得的 cx 标识；它不是 API Key。", "The cx identifier from a Programmable Search Engine; this is not an API key.")}
                placeholder={l("输入搜索引擎 ID（cx）", "Enter search engine ID (cx)")}
                configuredPlaceholder={l("输入新的搜索引擎 ID（留空则不更改）", "Enter a new search engine ID (leave blank to keep it)")}
                {...keyFieldProps("research", "GOOGLE_PSE_ENGINE_ID")}
              />
              <KeyField
                label={l("SearXNG 实例地址（SEARXNG_BASE_URL）", "SearXNG instance URL (SEARXNG_BASE_URL)")}
                hint={l("仅填写你有权使用并信任的 SearXNG 服务地址；该服务需支持 JSON 响应。费用、配额、隐私和数据规则请以实例运营方的当前说明为准。", "Enter only a trusted SearXNG service you are authorized to use; it must support JSON responses. Refer to the operator's current terms for pricing, quotas, privacy, and data practices.")}
                placeholder={l("输入服务地址", "Enter service URL")}
                configuredPlaceholder={l("输入新的服务地址（留空则不更改）", "Enter a new service URL (leave blank to keep it)")}
                {...keyFieldProps("research", "SEARXNG_BASE_URL")}
              />
              <OutboundToggle
                id="allow-ddg-fallback"
                title={l("允许使用 DuckDuckGo 备用搜索", "Allow DuckDuckGo fallback search")}
                description={l("它无需单独配置 API Key，但使用社区接口，稳定性和可用性可能不如已配置的搜索 API，因此只在其他搜索服务不可用时作为备用。使用限制、隐私和数据规则请以 DuckDuckGo 及社区接口的当前说明为准。", "This needs no separate API key but uses a community interface that may be less reliable than configured search APIs, so it is used only as a fallback. Refer to current DuckDuckGo and community-interface terms for limits, privacy, and data practices.")}
                checked={policyDraft.allow_ddg_fallback}
                disabled={busy}
                managedByEnvironment={state.environment_managed.outbound_policy.allow_ddg_fallback}
                status={policyDraft.allow_ddg_fallback ? l("已允许，仅作备用", "Allowed as fallback only") : l("已关闭，只用已配置的服务", "Off; use configured services only")}
                statusTone={policyDraft.allow_ddg_fallback ? "ok" : "muted"}
                onChange={(checked) => updatePolicyFlag("allow_ddg_fallback", checked)}
              />
              <OutboundToggle
                id="allow-deep-research"
                title={l("深度调研", "Deep research")}
                description={l("基于已配置的搜索服务。结果更全面，也会消耗更多配额。", "Uses configured search services for broader results and consumes more quota.")}
                checked={policyDraft.allow_deep_research}
                disabled={busy}
                managedByEnvironment={state.environment_managed.outbound_policy.allow_deep_research}
                status={policyDraft.allow_deep_research ? l("已开启，注意配额消耗", "On; monitor quota use") : l("未开启（默认省配额）", "Off (quota-saving default)")}
                statusTone={policyDraft.allow_deep_research ? "warn" : "muted"}
                onChange={(checked) => updatePolicyFlag("allow_deep_research", checked)}
              />
            </>
          )}
          {!policyDraft.allow_web_research && SEARCH_CREDENTIAL_NAMES.map((name) => (
            state.keys[name] ? (
              <StoredCredentialControl
                key={name}
                label={name}
                pendingClear={Boolean(clears.research[name])}
                disabled={busy || !state.credential_storage.available}
                credentialStorageUnavailable={!state.credential_storage.available}
                managedByEnvironment={state.environment_managed.keys[name] ?? false}
                onToggleClear={() => setClears((current) => ({
                  ...current,
                  research: {
                    ...current.research,
                    [name]: !current.research[name],
                  },
                }))}
              />
            ) : null
          ))}
        </div>
        {saveControls("research")}
      </section>
      <p className="text-xs leading-relaxed text-ink-3">
        {state.credential_storage.kind === "system"
          ? (
            <>
              {l(`若直接下载安装包并使用，你添加的 API Key 只保存在这台电脑官方的 ${state.credential_storage.label}，不会写入 CareerDesk.app、业务数据库或备份；下载更新后的安装包后，需要允许软件读取之前保存的API key以继续使用配置好的服务；模型与联网设置保存在本机配置文件中。`, `In an installed build, API keys are stored only in this computer's ${state.credential_storage.label}; they are not written to CareerDesk.app, the business database, or backups. After installing an update, allow CareerDesk to read previously saved keys to continue using configured services. Model and network settings remain in a local configuration file.`)}
            </>
          )
          : l("源码运行会把 API Key 保存到这台电脑的私有 .env 文件（权限 600），不会写入业务数据库。", "Source runs store API keys in this computer's private .env file (mode 600), not in the business database.")}
        <br />
        {l("第三方服务及名称只说明兼容性，不表示赞助、认可或合作。第三方服务由你自行选择和开通，其价格、配额、隐私政策和服务条款以服务商为准。", "Third-party names indicate compatibility only, not sponsorship, endorsement, or partnership. You choose and activate third-party services; their providers govern pricing, quotas, privacy policies, and terms.")}
      </p>
    </div>
  );
}

const SETTINGS_PAGES = [
  ["network", "network"],
  ["personal", "personal"],
  ["appearance", "appearance"],
] as const;

type SettingsPageId = (typeof SETTINGS_PAGES)[number][0];

const SECTION_PAGE: Record<string, SettingsPageId> = {
  privacy: "network",
  model: "network",
  retrieval: "network",
  research: "network",
  preferences: "personal",
  storage: "personal",
  appearance: "appearance",
  language: "appearance",
  experiments: "appearance",
};

function isSettingsPageId(value: string | null): value is SettingsPageId {
  return SETTINGS_PAGES.some(([id]) => id === value);
}

export function SettingsPage() {
  const t = useSettingsT();
  const [searchParams, setSearchParams] = useSearchParams();
  const requestedSection = searchParams.get("section");
  const requestedPage = isSettingsPageId(searchParams.get("page"))
    ? searchParams.get("page") as SettingsPageId
    : SECTION_PAGE[requestedSection ?? ""] ?? "network";
  const [activePage, setActivePage] = useState<SettingsPageId>(requestedPage);

  useEffect(() => {
    setActivePage(requestedPage);
    if (!requestedSection || SECTION_PAGE[requestedSection] !== requestedPage) return;
    const frame = window.requestAnimationFrame(() => {
      document.getElementById(`settings-${requestedSection}`)?.scrollIntoView({
        behavior: "auto",
        block: "start",
      });
    });
    return () => window.cancelAnimationFrame(frame);
  }, [requestedPage, requestedSection]);

  const openPage = (page: SettingsPageId) => {
    setActivePage(page);
    const next = new URLSearchParams(searchParams);
    next.set("page", page);
    next.delete("section");
    setSearchParams(next);
    window.requestAnimationFrame(() => window.scrollTo({ top: 0, behavior: "smooth" }));
  };

  return (
    <>
      <div className="sticky top-[49px] z-[8] -mx-4 -mt-6 mb-6 bg-surface/95 backdrop-blur md:top-0 md:-mx-8 md:-mt-9">
        <header className="px-4 py-4 md:px-8 md:py-5">
          <h1 className="text-[22px] font-semibold tracking-tight">{t("title")}</h1>
        </header>
        <nav
          aria-label={t("pages")}
          className="border-b border-line px-4 pb-2 md:px-8"
        >
          <div role="tablist" aria-label={t("pages")} className="grid grid-cols-3 gap-1 rounded-xl bg-panel-2 p-1">
            {SETTINGS_PAGES.map(([id, labelKey]) => (
              <button
                key={id}
                id={`settings-tab-${id}`}
                type="button"
                role="tab"
                aria-selected={activePage === id}
                aria-controls={`settings-panel-${id}`}
                className={`button-wrap min-w-0 rounded-lg px-1.5 py-2 text-center text-[11px] font-medium leading-tight transition-colors min-[380px]:px-2 min-[380px]:text-xs sm:text-sm ${
                  activePage === id
                    ? "bg-panel text-ink shadow-sm"
                    : "text-ink-2 hover:text-ink"
                }`}
                onClick={() => openPage(id)}
              >
                {t(labelKey)}
              </button>
            ))}
          </div>
        </nav>
      </div>
      <div
        id="settings-panel-network"
        role="tabpanel"
        aria-labelledby="settings-tab-network"
        hidden={activePage !== "network"}
      >
        <ModelAndPrivacySettingsSection />
      </div>
      <div
        id="settings-panel-personal"
        role="tabpanel"
        aria-labelledby="settings-tab-personal"
        hidden={activePage !== "personal"}
        className="flex min-w-0 flex-col gap-4"
      >
        <div id="settings-preferences" className="scroll-mt-48 min-[560px]:scroll-mt-36 md:scroll-mt-28">
          <PreferencesSettingsSection />
        </div>
        <div id="settings-storage" className="scroll-mt-48 min-[560px]:scroll-mt-36 md:scroll-mt-28">
          <StorageSettingsSection />
        </div>
      </div>
      <div
        id="settings-panel-appearance"
        role="tabpanel"
        aria-labelledby="settings-tab-appearance"
        hidden={activePage !== "appearance"}
        className="flex min-w-0 flex-col gap-4"
      >
        <LanguageSettingsSection />
        <ThemeSettingsSection />
        <GrillVisibilitySettingsSection />
      </div>
    </>
  );
}
