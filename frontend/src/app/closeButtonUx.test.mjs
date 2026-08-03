import assert from "node:assert/strict";
import { readdir, readFile } from "node:fs/promises";
import { test } from "node:test";
import ts from "typescript";

const sourceRootUrl = new URL("../", import.meta.url);

async function productionTsxFiles(directory) {
  const entries = await readdir(directory, { withFileTypes: true });
  const files = [];
  for (const entry of entries) {
    const entryUrl = new URL(entry.name, directory);
    if (entry.isDirectory()) {
      files.push(...await productionTsxFiles(new URL(`${entry.name}/`, directory)));
    } else if (entry.name.endsWith(".tsx") && !entry.name.endsWith(".test.tsx")) {
      files.push(entryUrl);
    }
  }
  return files;
}

function visibleButtonText(node) {
  if (!ts.isJsxElement(node) || node.openingElement.tagName.getText() !== "button") return [];
  return node.children
    .filter(ts.isJsxText)
    .map((child) => child.text.trim())
    .filter(Boolean);
}

test("close buttons use a cross while retaining descriptive accessible labels", async () => {
  for (const fileUrl of await productionTsxFiles(sourceRootUrl)) {
    const source = await readFile(fileUrl, "utf8");
    const sourceFile = ts.createSourceFile(
      fileUrl.pathname,
      source,
      ts.ScriptTarget.Latest,
      true,
      ts.ScriptKind.TSX,
    );
    function visit(node) {
      assert.ok(
        !visibleButtonText(node).includes("关闭"),
        `${fileUrl.pathname} contains a close button with a visible 关闭 label`,
      );
      ts.forEachChild(node, visit);
    }
    visit(sourceFile);
  }
});
