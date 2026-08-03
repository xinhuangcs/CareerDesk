export type RuntimeLocale = "zh-CN" | "en";
export type LocalizedRuntimeMessage = Readonly<{ zhCN: string; en: string }>;

let runtimeLocale: RuntimeLocale = "en";

export function setRuntimeLocale(locale: RuntimeLocale): void {
  runtimeLocale = locale;
}

export function getRuntimeLocale(): RuntimeLocale {
  return runtimeLocale;
}

export function localizeRuntimeMessage(
  message: string | LocalizedRuntimeMessage,
): string {
  if (typeof message === "string") return message;
  return runtimeLocale === "en" ? message.en : message.zhCN;
}
