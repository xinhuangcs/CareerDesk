import { lazy, Suspense } from "react";
import { Link, Navigate, Route, Routes } from "react-router-dom";
import { RouteErrorBoundary } from "./RouteErrorBoundary";
import { APP_ROUTE_PATHS } from "./routePaths";
import { useLocalizer } from "../i18n/useLocalizer";

const GrillLabPage = lazy(() =>
  import("../features/grill/GrillLabPage").then((module) => ({ default: module.GrillLabPage })),
);
const TimelinePage = lazy(() =>
  import("../features/timeline/TimelinePage").then((module) => ({ default: module.TimelinePage })),
);
const LibraryPage = lazy(() =>
  import("../features/library/LibraryPage").then((module) => ({ default: module.LibraryPage })),
);
const SettingsPage = lazy(() =>
  import("../features/settings/SettingsPage").then((module) => ({ default: module.SettingsPage })),
);

function RouteLoadingState() {
  const l = useLocalizer();
  return (
    <div
      role="status"
      aria-live="polite"
      aria-atomic="true"
      className="card flex min-h-48 items-center justify-center p-8 text-center"
    >
      <div>
        <span className="mx-auto block h-2 w-24 rounded-full bg-line-strong motion-safe:animate-pulse" />
        <p className="mt-3 text-sm text-ink-3">{l("正在加载…", "Loading…")}</p>
      </div>
    </div>
  );
}

function NotFoundPage() {
  const l = useLocalizer();
  return (
    <div className="card mx-auto max-w-xl p-8 text-center">
      <h1 className="text-lg font-semibold">{l("找不到这个页面", "Page not found")}</h1>
      <p className="mt-2 text-sm text-ink-3">{l("链接可能已失效。你的资料和求职记录不会受到影响。", "The link may have expired. Your profile and application records are unaffected.")}</p>
      <Link to={APP_ROUTE_PATHS.chat} className="btn-primary mt-5">{l("返回求职助手", "Back to Career Agent")}</Link>
    </div>
  );
}

export function RouteContent({ pathname }: { pathname: string }) {
  return (
    <RouteErrorBoundary key={pathname}>
      <Suspense fallback={<RouteLoadingState />}>
        <Routes>
          <Route path={APP_ROUTE_PATHS.chat} element={null} />
          <Route path={APP_ROUTE_PATHS.grill} element={<GrillLabPage />} />
          <Route path={APP_ROUTE_PATHS.timeline} element={<TimelinePage />} />
          <Route
            path={APP_ROUTE_PATHS.questions}
            element={<Navigate replace to={`${APP_ROUTE_PATHS.grill}?view=questions`} />}
          />
          <Route path={APP_ROUTE_PATHS.library} element={<LibraryPage />} />
          <Route path={APP_ROUTE_PATHS.settings} element={<SettingsPage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Routes>
      </Suspense>
    </RouteErrorBoundary>
  );
}
