import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { useLocalizer } from "../../i18n/useLocalizer";
import {
  grillNavigationIsVisible,
  saveGrillNavigationVisibility,
  subscribeToGrillVisibility,
} from "./grillVisibilityPreference";

export function GrillVisibilitySettingsSection() {
  const l = useLocalizer();
  const [visible, setVisible] = useState(grillNavigationIsVisible);
  const [storageError, setStorageError] = useState(false);

  useEffect(() => subscribeToGrillVisibility(setVisible), []);

  const updateVisibility = (next: boolean) => {
    if (!saveGrillNavigationVisibility(next)) {
      setStorageError(true);
      return;
    }
    setStorageError(false);
    setVisible(next);
  };

  return (
    <section
      id="settings-experiments"
      className="card scroll-mt-48 overflow-hidden min-[560px]:scroll-mt-36 md:scroll-mt-28"
      aria-labelledby="experiment-settings-title"
    >
      <div className="flex items-start gap-4 p-5">
        <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl bg-ember-soft text-ember">
          <svg viewBox="0 0 20 20" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <path d="M10.4 2.2c.4 2.4-.6 3.7-1.7 5C7.5 8.5 6.2 9.8 6.2 12a4.1 4.1 0 0 0 8.2.3c.1-2-1-3.8-2.1-5.1-.3 1.2-1 2-1.8 2.4-.3-2.3.2-4.9-.1-7.4Z" />
          </svg>
        </div>
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-center gap-2">
            <h2 id="experiment-settings-title" className="text-sm font-semibold">{l("功能显示", "Feature visibility")}</h2>
            <span className="rounded-full bg-ember-soft px-2 py-0.5 text-[11px] font-medium text-ember">{l("实验版", "Experimental")}</span>
          </div>
          <p className="mt-1 text-xs leading-relaxed text-ink-3">
            {l("选择是否在导航中显示实验功能。隐藏入口不会删除任何数据。", "Choose whether experimental features appear in navigation. Hiding an entry never deletes its data.")}
          </p>
        </div>
      </div>
      <div className="border-t border-line bg-panel-2/35 px-5 py-4">
        <div className="flex items-center gap-4">
          <label htmlFor="show-grill-navigation" className="min-w-0 flex-1 cursor-pointer">
            <span className="block text-sm font-medium text-ink">{l("在导航中显示拷打室", "Show Interview Lab in navigation")}</span>
            <span className="mt-0.5 block text-xs leading-relaxed text-ink-3">
              {visible ? l("入口当前可见，可以随时进入练习。", "The entry is visible and ready whenever you want to practice.") : l("入口已隐藏；需要时可在这里重新开启。", "The entry is hidden. You can restore it here at any time.")}
            </span>
          </label>
          <input
            id="show-grill-navigation"
            type="checkbox"
            role="switch"
            className="h-5 w-5 shrink-0 cursor-pointer accent-[var(--accent)]"
            checked={visible}
            onChange={(event) => updateVisibility(event.target.checked)}
          />
        </div>
        {storageError && (
          <p role="alert" className="mt-3 text-xs text-bad">
            {l("当前浏览器不允许保存此偏好，导航显示状态没有改变。", "Your browser could not save this preference, so navigation visibility was not changed.")}
          </p>
        )}
        {!visible && (
          <Link className="mt-3 inline-flex text-xs font-medium text-ink underline underline-offset-4" to="/grill">
            {l("仍然进入拷打室", "Open Interview Lab anyway")}
          </Link>
        )}
      </div>
    </section>
  );
}
