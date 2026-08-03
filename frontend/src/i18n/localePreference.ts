import { useSyncExternalStore } from "react";
import { translate, type UiLocale } from "./i18n.ts";
import { setRuntimeLocale } from "../shared/api/runtimeLocale.ts";

export type LocaleMode = "system" | UiLocale;

const LOCALE_STORAGE_KEY = "careerdesk.locale.mode.v1"; // gitleaks:allow -- public localStorage identifier
const LOCALE_EVENT = "careerdesk:locale-change";

type LocaleSnapshot = Readonly<{ mode: LocaleMode; locale: UiLocale }>;
type LocaleRuntime = Pick<Window, "localStorage" | "navigator" | "addEventListener" | "removeEventListener" | "dispatchEvent">;

let snapshot: LocaleSnapshot = { mode: "system", locale: "en" };
const listeners = new Set<() => void>();

export function detectSystemLocale(language: string): UiLocale {
  return language.toLowerCase().startsWith("zh") ? "zh-CN" : "en";
}

function resolveLocale(mode: LocaleMode, language: string): UiLocale {
  return mode === "system" ? detectSystemLocale(language) : mode;
}

function readMode(storage: Pick<Storage, "getItem">): LocaleMode {
  try {
    const saved = storage.getItem(LOCALE_STORAGE_KEY);
    return saved === "zh-CN" || saved === "en" || saved === "system" ? saved : "system";
  } catch {
    return "system";
  }
}

function applyDocumentLocale(locale: UiLocale, root: HTMLElement = document.documentElement): void {
  root.lang = locale;
  document.title = translate(locale, "meta.title");
  document.querySelector<HTMLMetaElement>('meta[name="description"]')
    ?.setAttribute("content", translate(locale, "meta.description"));
}

function setSnapshot(next: LocaleSnapshot, notify = true): void {
  const changed = snapshot.mode !== next.mode || snapshot.locale !== next.locale;
  snapshot = next;
  setRuntimeLocale(next.locale);
  applyDocumentLocale(next.locale);
  if (changed && notify) listeners.forEach((listener) => listener());
}

export function initializeLocale(runtime: Pick<Window, "localStorage" | "navigator"> = window): LocaleSnapshot {
  const mode = readMode(runtime.localStorage);
  const locale = resolveLocale(mode, runtime.navigator.language);
  setSnapshot({ mode, locale }, false);
  return snapshot;
}

export function saveLocaleMode(
  mode: LocaleMode,
  runtime: Pick<Window, "localStorage" | "navigator" | "dispatchEvent"> = window,
): LocaleSnapshot {
  try {
    runtime.localStorage.setItem(LOCALE_STORAGE_KEY, mode);
  } catch {
    // Storage restrictions only disable persistence; the current session still changes immediately.
  }
  setSnapshot({ mode, locale: resolveLocale(mode, runtime.navigator.language) });
  runtime.dispatchEvent(new Event(LOCALE_EVENT));
  return snapshot;
}

export function installLocaleSync(runtime: LocaleRuntime = window): () => void {
  const refresh = () => {
    const mode = readMode(runtime.localStorage);
    setSnapshot({ mode, locale: resolveLocale(mode, runtime.navigator.language) });
  };
  runtime.addEventListener("storage", refresh);
  runtime.addEventListener("languagechange", refresh);
  runtime.addEventListener(LOCALE_EVENT, refresh);
  return () => {
    runtime.removeEventListener("storage", refresh);
    runtime.removeEventListener("languagechange", refresh);
    runtime.removeEventListener(LOCALE_EVENT, refresh);
  };
}

export function getLocaleSnapshot(): LocaleSnapshot {
  return snapshot;
}

export function subscribeToLocale(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useLocale(): LocaleSnapshot {
  return useSyncExternalStore(subscribeToLocale, getLocaleSnapshot, getLocaleSnapshot);
}

export function currentOutputLocale(): UiLocale {
  return snapshot.locale;
}
