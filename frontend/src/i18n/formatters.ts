import type { UiLocale } from "./i18n";

type DateInput = Date | number | string;
type FormatProfile = "date" | "dateTime" | "monthDay" | "time";

const dateProfiles: Record<FormatProfile, Intl.DateTimeFormatOptions> = {
  date: { year: "numeric", month: "short", day: "numeric" },
  dateTime: { year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" },
  monthDay: { month: "short", day: "numeric" },
  time: { hour: "2-digit", minute: "2-digit" },
};

const dateFormatters = new Map<string, Intl.DateTimeFormat>();
const numberFormatters = new Map<string, Intl.NumberFormat>();
const relativeFormatters = new Map<UiLocale, Intl.RelativeTimeFormat>();

function dateFormatter(locale: UiLocale, profile: FormatProfile): Intl.DateTimeFormat {
  const key = `${locale}:${profile}`;
  let formatter = dateFormatters.get(key);
  if (!formatter) {
    formatter = new Intl.DateTimeFormat(locale, dateProfiles[profile]);
    dateFormatters.set(key, formatter);
  }
  return formatter;
}

export function formatDate(value: DateInput, locale: UiLocale, profile: FormatProfile = "date"): string {
  const date = value instanceof Date ? value : new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : dateFormatter(locale, profile).format(date);
}

export function formatNumber(value: number, locale: UiLocale, options: Intl.NumberFormatOptions = {}): string {
  const key = `${locale}:${JSON.stringify(options)}`;
  let formatter = numberFormatters.get(key);
  if (!formatter) {
    formatter = new Intl.NumberFormat(locale, options);
    numberFormatters.set(key, formatter);
  }
  return formatter.format(value);
}

export function formatRelativeTime(value: number, unit: Intl.RelativeTimeFormatUnit, locale: UiLocale): string {
  let formatter = relativeFormatters.get(locale);
  if (!formatter) {
    formatter = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
    relativeFormatters.set(locale, formatter);
  }
  return formatter.format(value, unit);
}
