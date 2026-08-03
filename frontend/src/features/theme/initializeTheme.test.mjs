import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  applyTheme,
  initializeTheme,
  nextTheme,
  saveThemePreference,
} from "./initializeTheme.ts";

function runtime(saved, systemDark = false) {
  return {
    localStorage: { getItem: () => saved },
    matchMedia: (query) => ({
      matches: systemDark,
      media: query,
    }),
  };
}

function cssBlock(css, selector) {
  const escapedSelector = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = css.match(new RegExp(`${escapedSelector}\\s*\\{([^}]*)\\}`));
  assert.ok(match, `missing CSS block: ${selector}`);
  return match[1];
}

function cssColor(block, token) {
  const match = block.match(new RegExp(`--${token}:\\s*(#[0-9a-f]{6});`, "i"));
  assert.ok(match, `missing hex color token: --${token}`);
  return match[1];
}

function relativeLuminance(hex) {
  const channels = hex.match(/[0-9a-f]{2}/gi).map((channel) => Number.parseInt(channel, 16) / 255);
  const linear = channels.map((channel) => (
    channel <= 0.04045 ? channel / 12.92 : ((channel + 0.055) / 1.055) ** 2.4
  ));
  return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2];
}

function contrastRatio(first, second) {
  const lighter = Math.max(relativeLuminance(first), relativeLuminance(second));
  const darker = Math.min(relativeLuminance(first), relativeLuminance(second));
  return (lighter + 0.05) / (darker + 0.05);
}

test("a stored dark preference is applied before React mounts", () => {
  const root = { dataset: {} };

  assert.equal(initializeTheme(root, runtime("dark", false)), "dark");
  assert.equal(root.dataset.theme, "dark");
});

test("a stored light preference overrides a dark system preference", () => {
  const root = { dataset: { theme: "dark" } };

  assert.equal(initializeTheme(root, runtime("light", true)), "light");
  assert.equal("theme" in root.dataset, false);
});

test("a stored cool preference is restored independently of the system preference", () => {
  const root = { dataset: {} };

  assert.equal(initializeTheme(root, runtime("cool", true)), "cool");
  assert.equal(root.dataset.theme, "cool");
});

test("the theme button cycles through light, cool, dark, and back to light", () => {
  assert.equal(nextTheme("light"), "cool");
  assert.equal(nextTheme("cool"), "dark");
  assert.equal(nextTheme("dark"), "light");
});

test("applying light removes the theme attribute while named themes set it", () => {
  const root = { dataset: { theme: "dark" } };

  applyTheme("cool", root);
  assert.equal(root.dataset.theme, "cool");
  applyTheme("light", root);
  assert.equal("theme" in root.dataset, false);
});

test("saving a theme applies it immediately and persists the explicit preference", () => {
  const root = { dataset: {} };
  const writes = [];

  saveThemePreference("cool", root, { setItem: (...args) => writes.push(args) });

  assert.equal(root.dataset.theme, "cool");
  assert.deepEqual(writes, [["theme", "cool"]]);
});

test("unavailable theme storage does not block the immediate visual change", () => {
  const root = { dataset: {} };

  saveThemePreference("dark", root, { setItem: () => { throw new Error("blocked"); } });

  assert.equal(root.dataset.theme, "dark");
});

test("the stylesheet pins the approved eye-comfort, cool-gray-blue, and graphite surfaces", () => {
  const css = readFileSync(new URL("../../index.css", import.meta.url), "utf8");
  const light = cssBlock(css, ":root");
  const cool = cssBlock(css, '[data-theme="cool"]');
  const dark = cssBlock(css, '[data-theme="dark"]');

  assert.deepEqual(
    [cssColor(light, "surface"), cssColor(light, "panel"), cssColor(light, "panel-2")],
    ["#f6f4ee", "#fffefa", "#eeece5"],
  );
  assert.deepEqual(
    [cssColor(cool, "surface"), cssColor(cool, "panel"), cssColor(cool, "panel-2")],
    ["#f2f5f9", "#fbfcfe", "#e6edf6"],
  );
  assert.deepEqual(
    [cssColor(dark, "surface"), cssColor(dark, "panel"), cssColor(dark, "panel-2")],
    ["#111419", "#191d23", "#222831"],
  );
});

test("every theme keeps text, actions, and semantic statuses at readable contrast", () => {
  const css = readFileSync(new URL("../../index.css", import.meta.url), "utf8");
  const themes = [
    ["light", cssBlock(css, ":root")],
    ["cool", cssBlock(css, '[data-theme="cool"]')],
    ["dark", cssBlock(css, '[data-theme="dark"]')],
  ];

  for (const [name, block] of themes) {
    const panel = cssColor(block, "panel");
    for (const token of ["ink", "ink-2", "ink-3"]) {
      assert.ok(
        contrastRatio(cssColor(block, token), panel) >= 4.5,
        `${name} --${token} must remain readable on --panel`,
      );
    }
    assert.ok(
      contrastRatio(cssColor(block, "accent"), cssColor(block, "accent-ink")) >= 4.5,
      `${name} action colors must remain readable`,
    );
    for (const token of ["ok", "warn", "bad", "info"]) {
      assert.ok(
        contrastRatio(cssColor(block, token), cssColor(block, `${token}-soft`)) >= 4.5,
        `${name} --${token} must remain readable on --${token}-soft`,
      );
    }
  }
});

test("disabled storage falls back to the system preference", () => {
  const root = { dataset: {} };
  const unavailableStorage = runtime(null, true);
  unavailableStorage.localStorage.getItem = () => {
    throw new Error("storage disabled");
  };

  assert.equal(initializeTheme(root, unavailableStorage), "dark");
  assert.equal(root.dataset.theme, "dark");
});

test("source index uses one render-blocking same-origin module and no inline script", () => {
  const html = readFileSync(new URL("../../../index.html", import.meta.url), "utf8");
  const scripts = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)];

  assert.equal(scripts.length, 1);
  const [, attributes, body] = scripts[0];
  assert.match(attributes, /\btype=["']module["']/i);
  assert.match(attributes, /\bsrc=["']\/src\/main\.tsx["']/i);
  assert.match(attributes, /\bblocking=["']render["']/i);
  assert.equal(body.trim(), "");
  assert.ok(scripts[0].index < html.indexOf("</head>"));
});

test("the entry restores the theme and locale before mounting React", () => {
  const source = readFileSync(new URL("../../main.tsx", import.meta.url), "utf8");
  const themeInitialization = source.indexOf("initializeTheme();");
  const localeInitialization = source.indexOf("initializeLocale();");
  const mount = source.indexOf("createRoot(document");

  assert.notEqual(themeInitialization, -1);
  assert.notEqual(localeInitialization, -1);
  assert.notEqual(mount, -1);
  assert.ok(themeInitialization < mount);
  assert.ok(localeInitialization < mount);
});
