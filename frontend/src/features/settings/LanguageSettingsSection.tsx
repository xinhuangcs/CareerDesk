import {
  saveLocaleMode,
  useLocale,
  type LocaleMode,
} from "../../i18n/localePreference";
import { useSettingsT, type SettingsCopyKey } from "./settingsCopy";

const OPTIONS: ReadonlyArray<{
  value: LocaleMode;
  labelKey: SettingsCopyKey;
  descriptionKey: SettingsCopyKey;
}> = [
  { value: "system", labelKey: "system", descriptionKey: "systemDescription" },
  { value: "zh-CN", labelKey: "zhCN", descriptionKey: "zhCNDescription" },
  { value: "en", labelKey: "en", descriptionKey: "enDescription" },
];

export function LanguageSettingsSection() {
  const t = useSettingsT();
  const { mode } = useLocale();

  return (
    <section
      id="settings-language"
      className="card scroll-mt-48 p-5 min-[560px]:scroll-mt-36 md:scroll-mt-28"
      aria-labelledby="language-settings-title"
    >
      <h2 id="language-settings-title" className="text-sm font-semibold">{t("languageTitle")}</h2>
      <p className="mt-1 text-xs leading-relaxed text-ink-3">{t("languageDescription")}</p>
      <div className="mt-4 grid gap-3 sm:grid-cols-3" role="group" aria-label={t("languageGroup")}>
        {OPTIONS.map((option) => {
          const selected = mode === option.value;
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
              onClick={() => saveLocaleMode(option.value)}
            >
              <span className="flex min-w-0 items-start justify-between gap-2">
                <span className="min-w-0 text-sm font-medium text-ink">{t(option.labelKey)}</span>
                {selected && <span className="shrink-0 text-[11px] font-medium text-ok">{t("current")}</span>}
              </span>
              <span className="mt-2 block text-xs leading-relaxed text-ink-3">{t(option.descriptionKey)}</span>
            </button>
          );
        })}
      </div>
    </section>
  );
}
