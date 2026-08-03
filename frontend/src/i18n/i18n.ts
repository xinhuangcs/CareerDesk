import { resources, type TranslationKey } from "./resources.ts";

export type UiLocale = keyof typeof resources;
export type TranslationParams = Readonly<Record<string, string | number>>;
export type Translate = (key: TranslationKey | string, params?: TranslationParams) => string;

const pluralRules = new Map<UiLocale, Intl.PluralRules>();

function pluralKey(locale: UiLocale, key: string, params?: TranslationParams): string {
  if (typeof params?.count !== "number") return key;
  let rules = pluralRules.get(locale);
  if (!rules) {
    rules = new Intl.PluralRules(locale);
    pluralRules.set(locale, rules);
  }
  const candidate = `${key}_${rules.select(params.count)}`;
  return candidate in resources[locale].translation ? candidate : key;
}

export function translate(locale: UiLocale, key: TranslationKey | string, params?: TranslationParams): string {
  const selectedKey = pluralKey(locale, key, params);
  const dictionary: Readonly<Record<string, string>> = resources[locale].translation;
  const template = dictionary[selectedKey];
  if (template === undefined || template.length === 0) {
    throw new Error(`Missing ${locale} translation: ${selectedKey}`);
  }
  return template.replace(/\{\{\s*([^},\s]+)[^}]*\}\}/g, (_match, name: string) => {
    const value = params?.[name];
    if (value === undefined) throw new Error(`Missing interpolation value ${name} for ${selectedKey}`);
    return String(value);
  });
}
