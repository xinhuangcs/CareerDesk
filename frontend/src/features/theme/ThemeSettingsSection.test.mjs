import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const sectionUrl = new URL("./ThemeSettingsSection.tsx", import.meta.url);
const appUrl = new URL("../../app/App.tsx", import.meta.url);
const resourcesUrl = new URL("./themeCopy.ts", import.meta.url);

test("theme choices live in the settings appearance section", async () => {
  const [source, resources] = await Promise.all([
    readFile(sectionUrl, "utf8"),
    readFile(resourcesUrl, "utf8"),
  ]);

  assert.match(source, /id="settings-appearance"/);
  assert.match(source, /aria-label=\{t\("group"\)\}/);
  assert.match(source, /labelKey: "light"/);
  assert.match(source, /labelKey: "cool"/);
  assert.match(source, /labelKey: "dark"/);
  assert.match(resources, /light: "护眼白"/);
  assert.match(resources, /light: "Warm Light"/);
  assert.match(source, /saveThemePreference\(next\)/);
  assert.match(source, /aria-pressed=\{selected\}/);
});

test("the app shell no longer owns a theme button or local-first footer copy", async () => {
  const source = await readFile(appUrl, "utf8");

  assert.doesNotMatch(source, /ThemeToggle|LocalBadge|useTheme/);
  assert.doesNotMatch(source, /本地优先 · 数据由你掌控/);
});
