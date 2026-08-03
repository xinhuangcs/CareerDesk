import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const stylesheetUrl = new URL("../index.css", import.meta.url);
const appShellUrl = new URL("./App.tsx", import.meta.url);
const entryUrl = new URL("../main.tsx", import.meta.url);
const settingsUrl = new URL("../features/settings/SettingsPage.tsx", import.meta.url);

function cssRule(source, selector) {
  const escapedSelector = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = source.match(new RegExp(`${escapedSelector}\\s*\\{([^}]+)\\}`));
  assert.ok(match, `missing ${selector} rule`);
  return match[1].replace(/\/\*[\s\S]*?\*\//g, "").replace(/\s+/g, " ");
}

test("the web app leaves vertical wheel scrolling to the native viewport", async () => {
  const [css, appShell, entry, settings] = await Promise.all([
    readFile(stylesheetUrl, "utf8"),
    readFile(appShellUrl, "utf8"),
    readFile(entryUrl, "utf8"),
    readFile(settingsUrl, "utf8"),
  ]);
  const htmlRule = cssRule(css, "html");

  assert.doesNotMatch(htmlRule, /overscroll-behavior\s*:/);
  assert.doesNotMatch(htmlRule, /overflow-y:\s*(?:hidden|clip)\s*;/);
  assert.doesNotMatch(
    `${entry}\n${appShell}\n${settings}`,
    /addEventListener\(\s*["'](?:wheel|touchmove)["']/,
  );
  assert.doesNotMatch(settings, /overflow-x-auto/);
});
