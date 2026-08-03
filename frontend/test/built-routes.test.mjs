import assert from "node:assert/strict";
import { dirname, normalize, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { test } from "node:test";
import { build } from "vite";

const UNSPLIT_INITIAL_JS_BYTES = 716_176;
// The bilingual shell adds both first-frame dictionaries while every feature dictionary stays deferred.
// Keep the initial graph at least 9% below the recorded unsplit baseline.
const MAX_INITIAL_JS_RATIO = 0.91;
const frontendRoot = resolve(dirname(fileURLToPath(import.meta.url)), "..");
const routePages = [
  ["grill", "GrillLabPage"],
  ["library", "LibraryPage"],
  ["settings", "SettingsPage"],
  ["timeline", "TimelinePage"],
];

function normalizedModuleId(moduleId) {
  return normalize(moduleId.replace(/[?#].*$/, ""));
}

function ownerChunk(chunks, modulePath) {
  const expected = normalize(modulePath);
  const owners = chunks.filter((chunk) =>
    Object.keys(chunk.modules).some((moduleId) => normalizedModuleId(moduleId) === expected));
  assert.equal(owners.length, 1, modulePath);
  return owners[0];
}

function staticClosure(entry, chunksByFileName) {
  const files = new Set();
  function visit(fileName) {
    if (files.has(fileName)) return;
    files.add(fileName);
    const chunk = chunksByFileName.get(fileName);
    assert.ok(chunk, `missing static chunk ${fileName}`);
    for (const imported of chunk.imports) visit(imported);
  }
  visit(entry.fileName);
  return files;
}

test("the production graph defers four route groups while keeping Chat in the initial graph", async (context) => {
  const result = await build({
    logLevel: "silent",
    build: { write: false, manifest: false },
  });
  const rollupOutputs = Array.isArray(result) ? result : [result];
  const outputs = rollupOutputs.flatMap((output) => output.output ?? []);
  const chunks = outputs.filter((output) => output.type === "chunk");
  const htmlAssets = outputs.filter(
    (output) => output.type === "asset" && output.fileName === "index.html",
  );
  assert.equal(htmlAssets.length, 1);
  assert.equal(typeof htmlAssets[0].source, "string");
  const html = htmlAssets[0].source;
  const entries = chunks.filter((chunk) => chunk.isEntry);
  assert.equal(entries.length, 1);
  const entry = entries[0];
  const chunksByFileName = new Map(chunks.map((chunk) => [chunk.fileName, chunk]));
  const initialFiles = staticClosure(entry, chunksByFileName);
  const initialChunks = [...initialFiles].map((fileName) => chunksByFileName.get(fileName));
  const dynamicTargets = new Set(initialChunks.flatMap((chunk) => chunk.dynamicImports));

  const chatPath = resolve(frontendRoot, "src/features/chat/ChatPage.tsx");
  const chatChunk = ownerChunk(chunks, chatPath);
  assert.ok(initialFiles.has(chatChunk.fileName), "Chat must stay in the initial graph");

  const routeChunks = routePages.map(([feature, page]) => {
    const chunk = ownerChunk(
      chunks,
      resolve(frontendRoot, `src/features/${feature}/${page}.tsx`),
    );
    assert.equal(chunk.isDynamicEntry, true, `${page} must be a dynamic entry`);
    assert.ok(!initialFiles.has(chunk.fileName), `${page} leaked into the initial graph`);
    assert.ok(dynamicTargets.has(chunk.fileName), `${page} must be an initial dynamic target`);
    return chunk;
  });
  assert.equal(new Set(routeChunks.map((chunk) => chunk.fileName)).size, routePages.length);

  const labChunk = ownerChunk(
    chunks,
    resolve(frontendRoot, "src/features/grill/GrillLabPage.tsx"),
  );
  for (const [feature, page] of [["grill", "GrillPage"], ["questions", "QuestionsPage"]]) {
    assert.equal(
      ownerChunk(chunks, resolve(frontendRoot, `src/features/${feature}/${page}.tsx`)).fileName,
      labChunk.fileName,
      `${page} must ship inside the deferred lab group`,
    );
  }

  const entrySource = html.match(
    /<script\b[^>]*\bsrc=["']\/([^"']+\.js)["'][^>]*>/i,
  )?.[1];
  assert.equal(entrySource, entry.fileName);

  for (const chunk of routeChunks) {
    assert.ok(!html.includes(chunk.fileName), `${chunk.fileName} must not be preloaded`);
  }

  const initialGraphBytes = initialChunks.reduce(
    (total, chunk) => total + Buffer.byteLength(chunk.code),
    0,
  );
  const ratio = initialGraphBytes / UNSPLIT_INITIAL_JS_BYTES;
  assert.ok(
    ratio <= MAX_INITIAL_JS_RATIO,
    `initial JS graph regressed to ${(ratio * 100).toFixed(2)}% of the unsplit baseline`,
  );

  const deferredRouteBytes = routeChunks.reduce(
    (total, chunk) => total + Buffer.byteLength(chunk.code),
    0,
  );
  context.diagnostic(
    `initial JS graph: ${initialGraphBytes} B (${((1 - ratio) * 100).toFixed(2)}% smaller); `
    + `four deferred route groups: ${deferredRouteBytes} B`,
  );
});
