import assert from "node:assert/strict";
import { readFileSync, statSync } from "node:fs";
import { test } from "node:test";

test("the production index remains compatible with script-src self", () => {
  const html = readFileSync(new URL("../dist/index.html", import.meta.url), "utf8");
  const scripts = [...html.matchAll(/<script\b([^>]*)>([\s\S]*?)<\/script>/gi)];

  assert.equal(scripts.length, 1);
  const [, attributes, body] = scripts[0];
  const source = attributes.match(/\bsrc=["']([^"']+)["']/i)?.[1];

  assert.equal(body.trim(), "", "production HTML must not contain executable inline script");
  assert.match(attributes, /\btype=["']module["']/i);
  assert.match(attributes, /\bblocking=["']render["']/i);
  assert.ok(source, "production module must have an external source");
  assert.match(source, /^\/assets\/[A-Za-z0-9._-]+\.js$/);
  assert.doesNotMatch(source, /^(?:[a-z][a-z\d+.-]*:)?\/\//i);
  const entryFile = statSync(new URL(`../dist${source}`, import.meta.url));
  assert.ok(entryFile.isFile());
  assert.ok(entryFile.size > 0);
  assert.ok(scripts[0].index < html.indexOf("</head>"));
});
