import { matchRoutes, type RouteObject } from "react-router-dom";

export const APP_ROUTE_PATHS = {
  chat: "/",
  grill: "/grill",
  timeline: "/timeline",
  questions: "/questions",
  library: "/library",
  settings: "/settings",
} as const;

export type AppRoutePath =
  (typeof APP_ROUTE_PATHS)[keyof typeof APP_ROUTE_PATHS];

type KnownRoute = RouteObject & {
  canonicalPathname: AppRoutePath;
};

// Keep canonicalization on the same matcher as <Routes>. In particular, React Router accepts
// case differences, trailing slashes and encoded unreserved characters. A hand-written lowercase/
// trim helper would eventually disagree with route rendering. There is deliberately no wildcard:
// unknown paths must remain unknown and render the 404 page unchanged.
const KNOWN_ROUTES: KnownRoute[] = Object.values(APP_ROUTE_PATHS).map(
  (canonicalPathname) => ({
    path: canonicalPathname,
    canonicalPathname,
  }),
);

export function canonicalKnownPathname(pathname: string): AppRoutePath | null {
  return matchRoutes<KnownRoute>(KNOWN_ROUTES, { pathname })
    ?.at(-1)?.route.canonicalPathname ?? null;
}
