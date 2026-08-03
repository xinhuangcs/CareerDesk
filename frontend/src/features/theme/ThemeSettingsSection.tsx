import { useState } from "react";
import { useThemeT, type ThemeCopyKey } from "./themeCopy";
import { saveThemePreference, type Theme } from "./initializeTheme";

const THEME_OPTIONS: ReadonlyArray<{
  value: Theme;
  labelKey: ThemeCopyKey;
  descriptionKey: ThemeCopyKey;
  swatches: readonly [string, string, string];
}> = [
  {
    value: "light",
    labelKey: "light",
    descriptionKey: "lightDescription",
    swatches: ["#f6f4ee", "#fffefa", "#24231f"],
  },
  {
    value: "cool",
    labelKey: "cool",
    descriptionKey: "coolDescription",
    swatches: ["#f2f5f9", "#fbfcfe", "#294867"],
  },
  {
    value: "dark",
    labelKey: "dark",
    descriptionKey: "darkDescription",
    swatches: ["#111419", "#191d23", "#e6ebf2"],
  },
];

function currentTheme(): Theme {
  const theme = document.documentElement.dataset.theme;
  return theme === "cool" || theme === "dark" ? theme : "light";
}

export function ThemeSettingsSection() {
  const t = useThemeT();
  const [theme, setTheme] = useState<Theme>(currentTheme);

  const selectTheme = (next: Theme) => {
    setTheme(next);
    saveThemePreference(next);
  };

  return (
    <section
      id="settings-appearance"
      className="card scroll-mt-48 p-5 min-[560px]:scroll-mt-36 md:scroll-mt-28"
      aria-labelledby="appearance-settings-title"
    >
      <h2 id="appearance-settings-title" className="text-sm font-semibold">{t("title")}</h2>
      <div className="mt-4 grid gap-3 sm:grid-cols-3" role="group" aria-label={t("group")}>
        {THEME_OPTIONS.map((option) => {
          const selected = theme === option.value;
          return (
            <button
              key={option.value}
              type="button"
              aria-pressed={selected}
              className={`button-wrap min-w-0 rounded-xl border p-3 text-left transition-[border-color,background-color,box-shadow] ${
                selected
                  ? "border-line-strong bg-panel-2 shadow-[var(--shadow-card)]"
                  : "border-line bg-panel hover:border-line-strong hover:bg-panel-2"
              }`}
              onClick={() => selectTheme(option.value)}
            >
              <span className="flex min-w-0 items-start justify-between gap-2">
                <span className="min-w-0 text-sm font-medium text-ink">{t(option.labelKey)}</span>
                {selected && <span className="shrink-0 text-[11px] font-medium text-ok">{t("current")}</span>}
              </span>
              <span className="mt-2 flex h-7 overflow-hidden rounded-lg border border-line" aria-hidden="true">
                {option.swatches.map((color) => (
                  <span key={color} className="flex-1" style={{ backgroundColor: color }} />
                ))}
              </span>
              <span className="mt-2 block text-xs leading-relaxed text-ink-3">{t(option.descriptionKey)}</span>
            </button>
          );
        })}
      </div>
    </section>
  );
}
