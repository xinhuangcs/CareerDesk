import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const chatPageUrl = new URL("./ChatPage.tsx", import.meta.url);
const stylesheetUrl = new URL("../../index.css", import.meta.url);

test("assistant handoff links render as outlined pill buttons with a direction cue", async () => {
  const [chatPage, stylesheet] = await Promise.all([
    readFile(chatPageUrl, "utf8"),
    readFile(stylesheetUrl, "utf8"),
  ]);

  assert.match(chatPage, /className="btn-secondary chat-ui-action"/);
  assert.match(chatPage, /<span aria-hidden="true">→<\/span>/);
  assert.match(stylesheet, /\.btn-secondary\s*\{[\s\S]*?border:\s*1px solid var\(--line-strong\)/);
  assert.match(stylesheet, /\.chat-ui-action\s*\{[\s\S]*?!rounded-full/);
  assert.match(stylesheet, /\.chat-ui-action:focus-visible\s*\{\s*border-radius:\s*9999px;/);
});
