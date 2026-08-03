import { useCallback } from "react";
import { useLocale } from "./localePreference";

export type Localize = (zhCN: string, en: string) => string;

/** Select one native copy slot without allocating dictionaries during renders. */
export function useLocalizer(): Localize {
  const { locale } = useLocale();
  return useCallback(
    (zhCN: string, en: string) => (locale === "en" ? en : zhCN),
    [locale],
  );
}
