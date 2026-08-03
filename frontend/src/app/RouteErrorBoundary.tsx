import { Component, type ReactNode } from "react";
import { Link } from "react-router-dom";
import { APP_ROUTE_PATHS } from "./routePaths";
import { useLocalizer } from "../i18n/useLocalizer";

type RouteErrorBoundaryProps = {
  children: ReactNode;
};

type RouteErrorBoundaryState = {
  failed: boolean;
};

function RouteErrorFallback() {
  const l = useLocalizer();
  return (
    <section
      role="alert"
      aria-labelledby="route-error-title"
      className="card mx-auto max-w-xl p-8 text-center"
    >
      <h2 id="route-error-title" className="text-lg font-semibold">
        {l("页面暂时无法加载", "This page could not be loaded")}
      </h2>
      <p className="mt-2 text-sm text-ink-3">
        {l("其他页面仍可使用。你可以先返回求职助手，或在确认没有未发送内容后重新加载应用。", "Other pages are still available. Return to Career Agent, or reload after checking that you have no unsent work.")}
      </p>
      <div className="mt-5 flex flex-wrap justify-center gap-2">
        <Link to={APP_ROUTE_PATHS.chat} className="btn-primary">
          {l("返回求职助手", "Back to Career Agent")}
        </Link>
        <button
          type="button"
          className="btn"
          aria-describedby="route-reload-warning"
          onClick={() => window.location.reload()}
        >
          {l("重新加载应用", "Reload app")}
        </button>
      </div>
      <p id="route-reload-warning" className="mt-3 text-xs text-warn">
        {l("重新加载会丢失尚未发送的草稿、附件，以及当前标签页中尚未完成的恢复信息。", "Reloading discards unsent drafts, attachments, and unfinished recovery state in this tab.")}
      </p>
    </section>
  );
}

/**
 * Keeps a failed optional route from taking down the shell or the always-mounted Chat page.
 * RouteContent remounts this boundary for each pathname. Retrying the same rejected chunk requires
 * an explicit reload because React.lazy caches the rejected import promise.
 */
export class RouteErrorBoundary extends Component<
  RouteErrorBoundaryProps,
  RouteErrorBoundaryState
> {
  state: RouteErrorBoundaryState = {
    failed: false,
  };

  static getDerivedStateFromError(): Partial<RouteErrorBoundaryState> {
    return { failed: true };
  }

  render() {
    if (!this.state.failed) return this.props.children;

    return <RouteErrorFallback />;
  }
}
