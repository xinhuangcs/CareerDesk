import { useLocale } from "../../i18n/localePreference";

const copy = {
  "zh-CN": { title: "外观", group: "界面主题", current: "当前", light: "护眼白", lightDescription: "暖米白背景，适合日常长时间阅读", cool: "冷灰蓝", coolDescription: "低饱和冷色，信息层级更清晰", dark: "深色", darkDescription: "石墨深色，适合弱光环境" },
  en: { title: "Appearance", group: "Interface theme", current: "Current", light: "Warm Light", lightDescription: "A warm off-white surface designed for comfortable long sessions", cool: "Cool Blue-Gray", coolDescription: "A low-saturation cool palette with crisp information hierarchy", dark: "Dark", darkDescription: "A graphite theme for low-light environments" },
} as const;

export type ThemeCopyKey = keyof typeof copy["zh-CN"];
export function useThemeT(): (key: ThemeCopyKey) => string {
  const { locale } = useLocale();
  return (key) => copy[locale][key];
}
