import { translate, type Translate } from "./i18n.ts";
import { useLocale } from "./localePreference.ts";

/** Returns a translator bound to the current reactive UI locale. */
export function useT(): Translate {
  const { locale } = useLocale();
  return (key, params) => translate(locale, key, params);
}
