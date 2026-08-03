type ThemeRuntime = Pick<Window, "localStorage" | "matchMedia">;
type ThemeRoot = Pick<HTMLElement, "dataset">;

export type Theme = "light" | "cool" | "dark";

const THEME_SEQUENCE: readonly Theme[] = ["light", "cool", "dark"];

export function applyTheme(theme: Theme, root: ThemeRoot = document.documentElement): void {
  if (theme === "light") {
    delete root.dataset.theme;
  } else {
    root.dataset.theme = theme;
  }
}

export function nextTheme(theme: Theme): Theme {
  const index = THEME_SEQUENCE.indexOf(theme);
  return THEME_SEQUENCE[(index + 1) % THEME_SEQUENCE.length];
}

export function saveThemePreference(
  theme: Theme,
  root: ThemeRoot = document.documentElement,
  storage: Pick<Storage, "setItem"> = window.localStorage,
): void {
  applyTheme(theme, root);
  try {
    storage.setItem("theme", theme);
  } catch {
    // Storage may be unavailable; the theme still applies immediately to this page.
  }
}

/** Restore theme before React mounts, falling back to system preference without blocking. */
export function initializeTheme(
  root: ThemeRoot = document.documentElement,
  runtime: ThemeRuntime = window,
): Theme {
  let saved: string | null = null;
  try {
    saved = runtime.localStorage.getItem("theme");
  } catch {
    // Sandbox or privacy mode may block storage; continue with system preference.
  }

  const prefersDark = typeof runtime.matchMedia === "function"
    && runtime.matchMedia("(prefers-color-scheme: dark)").matches;
  const theme: Theme = saved === "cool"
    ? "cool"
    : saved === "dark" || (saved !== "light" && prefersDark)
      ? "dark"
      : "light";

  applyTheme(theme, root);
  return theme;
}
