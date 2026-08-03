import assert from "node:assert/strict";
import { test } from "node:test";

import { markdownImageSourceKind } from "./remoteImages.ts";

test("explicit remote Markdown images never qualify for automatic rendering", () => {
  assert.equal(markdownImageSourceKind("https://tracker.example/pixel.png"), "remote");
  assert.equal(markdownImageSourceKind(" HTTP://example.com/image.jpg "), "remote");
  assert.equal(markdownImageSourceKind("//cdn.example/image.webp"), "remote");
  assert.equal(markdownImageSourceKind("\\\\cdn.example\\image.webp"), "remote");
});

test("unsafe non-http schemes are blocked instead of becoming image or link targets", () => {
  assert.equal(markdownImageSourceKind("javascript:alert(1)"), "blocked");
  assert.equal(markdownImageSourceKind("data:image/svg+xml;base64,PHN2Zz4="), "blocked");
  assert.equal(markdownImageSourceKind("file:///private/secret.png"), "blocked");
  assert.equal(markdownImageSourceKind(""), "blocked");
});

test("URL control characters cannot be normalized into a remote image URL", () => {
  for (const source of [
    "ht\ttps://tracker.example/pixel.png",
    "h\nttps://tracker.example/pixel.png",
    "\u0001https://tracker.example/pixel.png",
    "https://tracker.example/pixel.png\u007f",
  ]) {
    assert.equal(markdownImageSourceKind(source), "blocked");
  }
});

test("relative same-origin paths remain renderable", () => {
  assert.equal(markdownImageSourceKind("/api/files/preview.png"), "local");
  assert.equal(markdownImageSourceKind("./assets/diagram.png"), "local");
  assert.equal(markdownImageSourceKind("images/diagram.png"), "local");
});
