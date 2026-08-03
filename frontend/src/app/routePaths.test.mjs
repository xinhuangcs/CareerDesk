import assert from "node:assert/strict";
import { test } from "node:test";
import {
  APP_ROUTE_PATHS,
  canonicalKnownPathname,
} from "./routePaths.ts";

test("the route path allowlist is unique and canonical", () => {
  assert.deepEqual(APP_ROUTE_PATHS, {
    chat: "/",
    grill: "/grill",
    timeline: "/timeline",
    questions: "/questions",
    library: "/library",
    settings: "/settings",
  });
  assert.equal(
    new Set(Object.values(APP_ROUTE_PATHS)).size,
    Object.values(APP_ROUTE_PATHS).length,
  );

  for (const pathname of Object.values(APP_ROUTE_PATHS)) {
    assert.equal(canonicalKnownPathname(pathname), pathname);
  }
});

test("known React Router path variants resolve to one canonical pathname", () => {
  const cases = new Map([
    ["", APP_ROUTE_PATHS.chat],
    ["//", APP_ROUTE_PATHS.chat],
    ["///", APP_ROUTE_PATHS.chat],
    ["/GRILL", APP_ROUTE_PATHS.grill],
    ["/TIMELINE/", APP_ROUTE_PATHS.timeline],
    ["/que%73tions", APP_ROUTE_PATHS.questions],
    ["/LIBRARY///", APP_ROUTE_PATHS.library],
    ["/Settings", APP_ROUTE_PATHS.settings],
    ["/SeTTings///", APP_ROUTE_PATHS.settings],
    ["/%73ettings", APP_ROUTE_PATHS.settings],
  ]);

  for (const [pathname, expected] of cases) {
    assert.equal(canonicalKnownPathname(pathname), expected, pathname);
  }
});

test("unknown and structurally different paths are never rewritten", () => {
  for (const pathname of [
    "settings",
    "/UNKNOWN",
    "/unknown/",
    "/settings-extra",
    "/settings/profile",
    "//settings",
    "/settings%2F",
    "/%2Fsettings",
  ]) {
    assert.equal(canonicalKnownPathname(pathname), null, pathname);
  }
});
