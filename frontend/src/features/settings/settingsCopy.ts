import { useLocale } from "../../i18n/localePreference";

const copy = {
  "zh-CN": {
    title: "设置", pages: "设置页面", network: "联网与 AI", personal: "偏好与数据", appearance: "外观与功能",
    languageTitle: "语言", languageDescription: "界面语言会立即切换；新生成的 AI 内容使用切换后的语言，已有内容保持原样。",
    languageGroup: "界面语言", system: "跟随系统", systemDescription: "中文系统使用简体中文，其他系统使用 English",
    zhCN: "简体中文", zhCNDescription: "使用简体中文界面和中文 AI 输出", en: "English",
    enDescription: "Use the English interface and English AI output", current: "当前",
  },
  en: {
    title: "Settings", pages: "Settings pages", network: "Connections & AI", personal: "Preferences & Data", appearance: "Appearance & Features",
    languageTitle: "Language", languageDescription: "The interface changes immediately. New AI content uses the selected language; existing content stays unchanged.",
    languageGroup: "Interface language", system: "Use system language", systemDescription: "Chinese systems use Simplified Chinese; all other systems use English",
    zhCN: "简体中文", zhCNDescription: "使用简体中文界面和中文 AI 输出", en: "English",
    enDescription: "Use the English interface and English AI output", current: "Current",
  },
} as const;

export type SettingsCopyKey = keyof typeof copy["zh-CN"];
export function useSettingsT(): (key: SettingsCopyKey) => string {
  const { locale } = useLocale();
  return (key) => copy[locale][key];
}
