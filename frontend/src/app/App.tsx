import { useEffect, useRef, useState } from "react";
import { Link, NavLink, useLocation, useNavigate } from "react-router-dom";
import type { OutboundPolicy } from "../features/settings/outboundPolicy";
import { StorageDisclosure } from "../features/settings/StorageDisclosure";
import { installSystemTimeZoneSync } from "../features/settings/systemTimezoneSync";
import { Logo } from "../icons";
import { WRITE_HEADERS } from "../shared/api/headers";
import { ChatPage } from "../features/chat/ChatPage";
import {
  grillNavigationIsVisible,
  subscribeToGrillVisibility,
} from "../features/grill/grillVisibilityPreference";
import { RouteContent } from "./RouteContent";
import { APP_ROUTE_PATHS, canonicalKnownPathname } from "./routePaths";
import { installLocaleSync } from "../i18n/localePreference";
import { useT } from "../i18n/useT";


type IconName = "chat" | "flame" | "board" | "folder" | "sliders";

function Icon({ name }: { name: IconName }) {
  const paths: Record<IconName, string> = {
    chat: "M14 3H4a1.5 1.5 0 0 0-1.5 1.5v6A1.5 1.5 0 0 0 4 12h1v2.5L8.2 12H14a1.5 1.5 0 0 0 1.5-1.5v-6A1.5 1.5 0 0 0 14 3Z",
    flame: "M9 2.5c.4 2-.4 3.1-1.4 4.2C6.5 7.9 5.2 9 5.2 11a3.9 3.9 0 0 0 7.8.3c.1-1.8-.8-3.4-1.7-4.5-.3 1-.9 1.6-1.6 2-.2-2 .1-4.4-.7-6.3Z",
    board: "M3 3.5h3.4v9H3zM7.3 3.5h3.4v6H7.3zM11.6 3.5H15v11h-3.4z",
    folder: "M2.5 4.5A1.5 1.5 0 0 1 4 3h3l1.5 2H14a1.5 1.5 0 0 1 1.5 1.5V12A1.5 1.5 0 0 1 14 13.5H4A1.5 1.5 0 0 1 2.5 12z",
    sliders: "M2.5 5h4.9M10.6 5h4.9M10.6 5a1.6 1.6 0 1 1-3.2 0 1.6 1.6 0 1 1 3.2 0M2.5 12h2.8M8.5 12h6.9M8.5 12a1.6 1.6 0 1 1-3.2 0 1.6 1.6 0 1 1 3.2 0",
  };
  return (
    <svg viewBox="0 0 18 17" className="h-4 w-4 shrink-0" fill="none"
         stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <path d={paths[name]} />
    </svg>
  );
}

type NavEntry = { to: string; end?: boolean; labelKey: string; icon: IconName; badgeKey?: string };

const SIDEBAR_COLLAPSED_KEY = "careerdesk.sidebar.collapsed";

function initialSidebarCollapsed() {
  if (typeof window === "undefined") return false;
  try {
    const stored = window.localStorage.getItem(SIDEBAR_COLLAPSED_KEY);
    if (stored !== null) return stored === "1";
  } catch {
  }
  return window.matchMedia("(min-width: 768px) and (max-width: 1023px)").matches;
}

const NAV_PRIMARY: NavEntry[] = [
  { to: APP_ROUTE_PATHS.chat, end: true, labelKey: "shell.nav.chat", icon: "chat" },
  { to: APP_ROUTE_PATHS.timeline, labelKey: "shell.nav.timeline", icon: "board" },
];
const NAV_LAB: NavEntry[] = [
  { to: APP_ROUTE_PATHS.grill, labelKey: "shell.nav.grill", icon: "flame", badgeKey: "shell.experimental" },
];
const NAV_MANAGEMENT: NavEntry[] = [
  { to: APP_ROUTE_PATHS.library, labelKey: "shell.nav.library", icon: "folder" },
  { to: APP_ROUTE_PATHS.settings, labelKey: "shell.nav.settings", icon: "sliders" },
];

const PAGE_META: Record<string, { titleKey: string; hintKey: string; badgeKey?: string }> = {
  [APP_ROUTE_PATHS.grill]: {
    titleKey: "shell.page.grill.title",
    hintKey: "shell.page.grill.hint",
    badgeKey: "shell.experimental",
  },
  [APP_ROUTE_PATHS.library]: { titleKey: "shell.nav.library", hintKey: "shell.page.library.hint" },
  [APP_ROUTE_PATHS.settings]: { titleKey: "shell.nav.settings", hintKey: "shell.page.settings.hint" },
};

function Wordmark() {
  return (
    <div className="flex items-center gap-2.5 text-ink">
      <Logo className="h-7 w-7 shrink-0" />
      <span className="text-[15px] font-semibold tracking-tight">CareerDesk</span>
    </div>
  );
}

type ModelStatus = {
  editable: boolean;
  llm_model: string | null;
  llm_model_local: boolean | null;
  llm_capabilities: {
    source: "provider" | "configured" | "missing" | null;
  };
  outbound_policy: OutboundPolicy;
};

/** Shows setup and strict-offline guidance when AI work is unavailable or constrained. */
function ModelSetupBanner({ hidden }: { hidden: boolean }) {
  const t = useT();
  const [state, setState] = useState<ModelStatus | null>(null);

  useEffect(() => {
    if (hidden) return;
    fetch("/api/settings")
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json() as Promise<ModelStatus>;
      })
      .then(setState)
      .catch(() => {});
  }, [hidden]);

  if (hidden || !state) return null;
  const noModel = state.llm_model === null;
  const missingCapabilities = !noModel && state.llm_capabilities.source === "missing";
  const strictOffline = state.outbound_policy.strict_offline;
  if (!noModel && !missingCapabilities && !strictOffline) return null;
  const knownLocalModel = state.llm_model_local === true;
  const title = noModel
    ? t("shell.model.noModel.title")
    : missingCapabilities
      ? t("shell.model.missingCapacity.title")
      : t("shell.model.offline.title");
  const explanation = noModel
    ? `${t("shell.model.noModel")} ${
      state.editable
        ? strictOffline
          ? t("shell.model.chooseLocal")
          : t("shell.model.chooseAny")
        : t("shell.model.contactAdmin")
    }`
    : missingCapabilities
      ? t("shell.model.missingCapacity")
    : knownLocalModel
      ? t("shell.model.localOffline")
      : t("shell.model.cloudOffline");
  return (
    <div
      role="status"
      className="mb-6 flex flex-col gap-3 rounded-2xl border border-warn/30 bg-warn-soft p-4 text-sm sm:flex-row sm:items-center sm:justify-between"
    >
      <div>
        <p className="font-medium text-ink">{title}</p>
        <p className="mt-0.5 text-ink-2">{explanation}</p>
      </div>
      {state.editable && (
        <Link to={APP_ROUTE_PATHS.settings} className="btn-primary shrink-0">
          {noModel ? t("shell.model.configure") : missingCapabilities ? t("shell.model.capacity") : t("shell.model.privacy")}
        </Link>
      )}
    </div>
  );
}

// Upgrade reconciliation only restores safe metadata and never calls a model silently.
type UpgradeStatus = { derive_pending: boolean; pending_count: number };

function MaintenanceBanner() {
  const t = useT();
  const [status, setStatus] = useState<UpgradeStatus | null>(null);
  const [busy, setBusy] = useState(false);
  const [dismissed, setDismissed] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    fetch("/api/maintenance/status")
      .then((r) => r.json())
      .then((s: UpgradeStatus) => {
        if (s.derive_pending) setStatus(s);
      })
      .catch(() => {});
  }, []);

  if (!status || dismissed) return null;

  async function apply() {
    setBusy(true);
    setError("");
    try {
      const r = await fetch("/api/maintenance/reconcile", {
        method: "POST",
        headers: WRITE_HEADERS,
      });
      const result = (await r.json().catch(() => null)) as { status?: string; message?: string } | null;
      if (!r.ok || result?.status === "error") {
        throw new Error(result?.message || t("shell.maintenance.httpError", { status: r.status }));
      }
      setStatus(null);
    } catch (e) {
      setError(e instanceof Error ? e.message : t("shell.maintenance.error"));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mb-6 flex flex-col gap-3 rounded-2xl border border-info/25 bg-info-soft p-4 text-sm sm:flex-row sm:items-center sm:justify-between">
      <div className="flex items-start gap-2.5">
        <svg viewBox="0 0 16 16" className="mt-0.5 h-4 w-4 shrink-0 text-info" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
          <path d="M13.5 8a5.5 5.5 0 1 1-1.6-3.9M13.5 3v2.4h-2.4" />
        </svg>
        <span className="text-ink-2">
          {t("shell.maintenance.available", { count: status.pending_count })}
          {error && <span role="alert" className="mt-1 block text-bad">{error}</span>}
        </span>
      </div>
      <div className="flex shrink-0 gap-2">
        <button onClick={() => void apply()} disabled={busy} className="btn-primary btn-sm">
          {busy ? t("shell.maintenance.running") : t("shell.maintenance.start")}
        </button>
        <button onClick={() => setDismissed(true)} className="btn btn-sm">
          {t("shell.maintenance.later")}
        </button>
      </div>
    </div>
  );
}

export function App() {
  const t = useT();
  const location = useLocation();
  const navigate = useNavigate();
  const mobileNavRef = useRef<HTMLElement | null>(null);
  const [grillNavigationVisible, setGrillNavigationVisible] = useState(
    grillNavigationIsVisible,
  );
  const [sidebarCollapsed, setSidebarCollapsed] = useState(initialSidebarCollapsed);
  const canonicalPathname = canonicalKnownPathname(location.pathname);
  const effectivePathname = canonicalPathname ?? location.pathname;
  const isChatRoute = effectivePathname === APP_ROUTE_PATHS.chat;
  const isTimelineRoute = effectivePathname === APP_ROUTE_PATHS.timeline;
  const meta = PAGE_META[effectivePathname];
  // Timeline uses the available width while focused routes retain readable line lengths.
  const mainWidth = effectivePathname === APP_ROUTE_PATHS.timeline
    ? "max-w-[1760px]"
    : effectivePathname === APP_ROUTE_PATHS.settings
      ? "max-w-6xl"
      : "max-w-5xl";
  const visibleLabNavigation = grillNavigationVisible ? NAV_LAB : [];

  useEffect(() => installSystemTimeZoneSync(), []);
  useEffect(() => installLocaleSync(), []);

  const sideLink = ({ isActive }: { isActive: boolean }) => {
    const layout = sidebarCollapsed
      ? "mx-auto h-10 w-10 justify-center rounded-[11px] p-0"
      : "gap-2 rounded-xl px-2.5 py-2";
    const state = isActive
      ? "border-transparent bg-panel-2 text-ink"
      : sidebarCollapsed
        ? "border-transparent text-ink-3 hover:bg-panel-2 hover:text-ink active:scale-[0.96]"
        : "border-transparent text-ink-2 hover:bg-panel-2 hover:text-ink active:translate-x-px";
    return `group relative flex items-center border text-sm font-medium transition-[background-color,border-color,color,box-shadow,transform] ${layout} ${state}`;
  };
  const topLink = ({ isActive }: { isActive: boolean }) =>
    `flex items-center gap-1.5 rounded-full px-3 py-1.5 text-sm font-medium transition-colors ${
      isActive ? "bg-accent text-accent-ink" : "text-ink-2 hover:bg-panel-2"
    }`;

  useEffect(() => {
    if (canonicalPathname === null || canonicalPathname === location.pathname) return;
    void navigate(
      {
        pathname: canonicalPathname,
        search: location.search,
        hash: location.hash,
      },
      {
        replace: true,
        state: location.state,
      },
    );
  }, [
    canonicalPathname,
    location.hash,
    location.pathname,
    location.search,
    location.state,
    navigate,
  ]);

  useEffect(() => {
    mobileNavRef.current?.querySelector<HTMLElement>('[aria-current="page"]')
      ?.scrollIntoView({ behavior: "smooth", block: "nearest", inline: "center" });
  }, [effectivePathname]);

  useEffect(
    () => subscribeToGrillVisibility(setGrillNavigationVisible),
    [],
  );

  function toggleSidebar() {
    setSidebarCollapsed((collapsed) => {
      const next = !collapsed;
      try {
        window.localStorage.setItem(SIDEBAR_COLLAPSED_KEY, next ? "1" : "0");
      } catch {
        // Storage restrictions only disable persistence for later sessions.
      }
      return next;
    });
  }

  return (
    <div className={`min-h-screen md:flex ${isTimelineRoute ? "md:h-screen md:min-h-0 md:overflow-hidden" : ""}`}>
      <aside
        className={`sticky top-0 hidden h-screen shrink-0 flex-col border-r border-line bg-panel px-2.5 py-4 transition-[width] duration-200 ease-out md:flex ${
          sidebarCollapsed ? "w-16" : "w-52"
        }`}
      >
        <div className={`flex h-7 items-center ${sidebarCollapsed ? "mb-5 justify-center" : "mb-7 px-2"}`}>
          {sidebarCollapsed ? <Logo className="h-7 w-7 shrink-0 text-ink" /> : <Wordmark />}
        </div>
        <div id="desktop-sidebar-navigation" className="flex min-h-0 flex-1 flex-col">
          <nav aria-label={t("shell.nav.workspace")} className={`flex flex-col ${sidebarCollapsed ? "gap-1" : "gap-0.5"}`}>
            {NAV_PRIMARY.map((n) => (
              <NavLink
                key={n.to}
                to={n.to}
                end={n.end}
                className={sideLink}
                aria-label={sidebarCollapsed ? t(n.labelKey) : undefined}
                title={sidebarCollapsed ? t(n.labelKey) : undefined}
              >
                <Icon name={n.icon} />
                {!sidebarCollapsed && <span className="min-w-0 truncate">{t(n.labelKey)}</span>}
              </NavLink>
            ))}
          </nav>
          {visibleLabNavigation.length > 0 && (
            <>
              {sidebarCollapsed
                ? <div className="mx-3.5 my-4 h-px bg-line-2" aria-hidden="true" />
                : <div className="mb-1.5 mt-6 px-2.5"><span className="section-label">{t("shell.nav.lab")}</span></div>}
              <nav aria-label={t("shell.nav.lab")} className={`flex flex-col ${sidebarCollapsed ? "gap-1" : "gap-0.5"}`}>
                {visibleLabNavigation.map((n) => (
                  <NavLink
                    key={n.to}
                    to={n.to}
                    end={n.end}
                    className={sideLink}
                    aria-label={sidebarCollapsed ? t(n.labelKey) : undefined}
                    title={sidebarCollapsed ? `${t(n.labelKey)} (${n.badgeKey ? t(n.badgeKey) : ""})` : undefined}
                  >
                    <Icon name={n.icon} />
                    {!sidebarCollapsed && <span className="min-w-0 truncate">{t(n.labelKey)}</span>}
                    {!sidebarCollapsed && n.badgeKey && (
                      <span className="ml-auto rounded-full bg-ember-soft px-1.5 py-0.5 text-[10px] font-medium text-ember">
                        {t(n.badgeKey)}
                      </span>
                    )}
                  </NavLink>
                ))}
              </nav>
            </>
          )}
          {sidebarCollapsed
            ? <div className="mx-3.5 my-4 h-px bg-line-2" aria-hidden="true" />
            : <div className="mb-1.5 mt-6 px-2.5"><span className="section-label">{t("shell.nav.management")}</span></div>}
          <nav aria-label={t("shell.nav.management")} className={`flex flex-col ${sidebarCollapsed ? "gap-1" : "gap-0.5"}`}>
            {NAV_MANAGEMENT.map((n) => (
              <NavLink
                key={n.to}
                to={n.to}
                end={n.end}
                className={sideLink}
                aria-label={sidebarCollapsed ? t(n.labelKey) : undefined}
                title={sidebarCollapsed ? t(n.labelKey) : undefined}
              >
                <Icon name={n.icon} />
                {!sidebarCollapsed && <span className="min-w-0 truncate">{t(n.labelKey)}</span>}
              </NavLink>
            ))}
          </nav>
        </div>
        <button
          type="button"
          onClick={toggleSidebar}
          aria-controls="desktop-sidebar-navigation"
          aria-expanded={!sidebarCollapsed}
          aria-label={sidebarCollapsed ? t("shell.sidebar.expand") : t("shell.sidebar.collapse")}
          title={sidebarCollapsed ? t("shell.sidebar.expand") : t("shell.sidebar.collapse")}
          className="sidebar-toggle absolute right-0 top-1/2 z-20 flex h-7 w-[18px] translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-[7px] border border-line bg-panel/95 text-ink-3 shadow-[var(--shadow-card)] backdrop-blur transition-[background-color,border-color,color,transform] hover:border-line-strong hover:bg-panel hover:text-ink active:scale-95"
        >
          <svg
            viewBox="0 0 16 16"
            aria-hidden="true"
            className={`h-2.5 w-2.5 transition-transform duration-200 ${sidebarCollapsed ? "rotate-180" : ""}`}
            fill="none"
            stroke="currentColor"
            strokeWidth="1.6"
            strokeLinecap="round"
            strokeLinejoin="round"
          >
            <path d="m9.5 4-4 4 4 4" />
          </svg>
        </button>
      </aside>

      {/* Mobile: horizontally scrollable top navigation. */}
      <header className="sticky top-0 z-10 border-b border-line bg-panel/85 backdrop-blur md:hidden">
        <div className={`flex items-center gap-3 overflow-x-auto px-4 py-2.5 ${isChatRoute ? "mr-24" : ""}`}>
          <Logo className="h-7 w-7 shrink-0 text-ink" />
          <nav ref={mobileNavRef} aria-label={t("shell.nav.main")} className="flex shrink-0 items-center gap-1">
            {NAV_PRIMARY.map((n) => (
              <NavLink key={n.to} to={n.to} end={n.end} className={topLink}>
                {t(n.labelKey)}
              </NavLink>
            ))}
            {visibleLabNavigation.length > 0 && (
              <>
                <span className="mx-0.5 h-4 w-px shrink-0 bg-line" />
                {visibleLabNavigation.map((n) => (
                  <NavLink key={n.to} to={n.to} end={n.end} className={topLink}>
                    {t(n.labelKey)}
                    {n.badgeKey && <span className="text-[10px] opacity-75">{t(n.badgeKey)}</span>}
                  </NavLink>
                ))}
              </>
            )}
            <span className="mx-0.5 h-4 w-px shrink-0 bg-line" />
            {NAV_MANAGEMENT.map((n) => (
              <NavLink key={n.to} to={n.to} end={n.end} className={topLink}>
                {t(n.labelKey)}
              </NavLink>
            ))}
          </nav>
        </div>
      </header>

      <div className={`min-w-0 flex-1 ${isTimelineRoute ? "md:min-h-0" : ""}`}>
        <main className={`mx-auto ${mainWidth} px-4 py-6 md:px-8 md:py-9 ${isTimelineRoute ? "md:flex md:h-full md:min-h-0 md:flex-col md:overflow-y-auto" : ""}`}>
          <StorageDisclosure hidden={effectivePathname === APP_ROUTE_PATHS.settings} />
          <ModelSetupBanner hidden={effectivePathname === APP_ROUTE_PATHS.settings} />
          <MaintenanceBanner />
          {meta && effectivePathname !== APP_ROUTE_PATHS.settings && (
            <div className="mb-6">
              <div className="flex items-center gap-2">
                <h1 className="text-[22px] font-semibold tracking-tight">{t(meta.titleKey)}</h1>
                {meta.badgeKey && (
                  <span className="rounded-full bg-ember-soft px-2 py-0.5 text-xs font-medium text-ember">
                    {t(meta.badgeKey)}
                  </span>
                )}
              </div>
              <p className="mt-1 text-sm text-ink-3">{t(meta.hintKey)}</p>
            </div>
          )}
          {/*
            Chat 是带未发送草稿、附件与 durable turn ID 的内存 outbox。把它放在 Routes
            外常驻挂载，站内查看时间线/题库/设置时只隐藏，不因路由卸载而丢恢复现场；
            不把敏感正文持久化到浏览器存储。整页关闭/刷新时 React 不会运行卸载清理，
            已上传未发送的临时附件由服务端 24 小时 TTL 与每用户容量上限兜底回收。
          */}
          <div className={isChatRoute ? "" : "hidden"} aria-hidden={!isChatRoute}>
            <ChatPage active={isChatRoute} />
          </div>
          <div className={isTimelineRoute ? "md:min-h-64 md:flex-1" : ""}>
            <RouteContent pathname={effectivePathname} />
          </div>
        </main>
      </div>
    </div>
  );
}
