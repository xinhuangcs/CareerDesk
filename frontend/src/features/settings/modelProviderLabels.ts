import type { UiLocale } from "../../i18n/i18n";

type ProviderLabelSource = Readonly<{
  name: string;
  label: string;
}>;

const ENGLISH_PROVIDER_LABELS: Readonly<Record<string, string>> = Object.freeze({
  openai: "OpenAI",
  anthropic: "Anthropic Claude",
  gemini: "Google Gemini",
  deepseek: "DeepSeek",
  dashscope: "Alibaba Cloud Model Studio Qwen",
  moonshot: "Moonshot AI Kimi",
  zhipu: "Zhipu AI GLM",
  gemini_openai: "Gemini (OpenAI-compatible)",
  modelscope: "ModelScope",
  openai_compatible: "Generic OpenAI-compatible API",
  ollama: "Ollama",
  vllm: "vLLM",
  sglang: "SGLang",
});

export function modelProviderLabel(provider: ProviderLabelSource, locale: UiLocale): string {
  if (locale !== "en") return provider.label;
  return ENGLISH_PROVIDER_LABELS[provider.name] ?? provider.name;
}
