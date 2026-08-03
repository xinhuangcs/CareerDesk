import assert from "node:assert/strict";
import { readdirSync, readFileSync, statSync } from "node:fs";
import { dirname, extname, join, relative } from "node:path";
import { fileURLToPath } from "node:url";
import test from "node:test";
import ts from "typescript";

const srcRoot = join(dirname(fileURLToPath(import.meta.url)), "..");
const frontendRoot = dirname(srcRoot);
const cjk = /[\u3400-\u9fff]/u;
const copySelectors = new Set(["l", "t", "localize", "localized", "pickLocaleCopy"]);

function sourceFiles(directory) {
  return readdirSync(directory).flatMap((name) => {
    const path = join(directory, name);
    if (statSync(path).isDirectory()) return sourceFiles(path);
    if (![".ts", ".tsx", ".css"].includes(extname(path)) || path.includes(".test.")) return [];
    return [path];
  });
}

function commentFiles(directory) {
  return readdirSync(directory).flatMap((name) => {
    const path = join(directory, name);
    if (statSync(path).isDirectory()) return commentFiles(path);
    return [".ts", ".tsx", ".js", ".mjs", ".css"].includes(extname(path)) ? [path] : [];
  });
}

function frontendCommentFiles() {
  return [
    ...commentFiles(srcRoot),
    ...commentFiles(join(frontendRoot, "scripts")),
    ...commentFiles(join(frontendRoot, "test")),
    join(frontendRoot, "vite.config.ts"),
  ];
}

function stringValues(node) {
  const values = [];
  function collect(child) {
    if (ts.isStringLiteralLike(child) || ts.isJsxText(child)) values.push(child.text);
    ts.forEachChild(child, collect);
  }
  collect(node);
  return values;
}

function hasNativePair(node) {
  const values = stringValues(node);
  return values.some((value) => cjk.test(value))
    && values.some((value) => /[A-Za-z]/u.test(value) && !cjk.test(value));
}

function enclosingVariable(node) {
  for (let parent = node.parent; parent && !ts.isSourceFile(parent); parent = parent.parent) {
    if (ts.isVariableDeclaration(parent) && ts.isIdentifier(parent.name)) return parent.name.text;
  }
  return null;
}

function sourceHasPairedVariable(name, sourceFile) {
  if (!name) return false;
  const base = name.replace(/_(ZH|ZH_CN|EN)$/u, "");
  const candidates = new Set([`${base}_ZH`, `${base}_ZH_CN`, `${base}_EN`]);
  let matched = false;
  function visit(node) {
    if (ts.isVariableDeclaration(node) && ts.isIdentifier(node.name)
        && node.name.text !== name && candidates.has(node.name.text)) matched = true;
    if (!matched) ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return matched;
}

function isLocalizedLiteral(node, sourceFile) {
  if (sourceHasPairedVariable(enclosingVariable(node), sourceFile)) return true;
  for (let parent = node.parent; parent; parent = parent.parent) {
    if (ts.isCallExpression(parent)) {
      const callee = parent.expression.getText(sourceFile);
      if (copySelectors.has(callee) && parent.arguments.slice(0, 2).includes(node)) return true;
    }
    if (ts.isArrayLiteralExpression(parent) && parent.elements.length >= 2) {
      const values = parent.elements.slice(0, 2)
        .filter(ts.isStringLiteralLike).map((item) => item.text);
      if (values.length === 2 && cjk.test(values[0]) && !cjk.test(values[1])) return true;
    }
    if ((ts.isConditionalExpression(parent) || ts.isObjectLiteralExpression(parent))
        && hasNativePair(parent)) return true;
    if (ts.isSourceFile(parent)) break;
  }
  return false;
}

test("display copy has no unpaired CJK literals outside narrow data compatibility files", () => {
  const violations = [];
  for (const path of sourceFiles(srcRoot).filter((file) => [".ts", ".tsx"].includes(extname(file)))) {
    const file = relative(srcRoot, path);
    // Contracts and API modules contain compatibility aliases and internal diagnostics. The
    // display-layer audit intentionally targets rendered components; boundary tests separately
    // ensure transport details never leak into UI copy.
    if (extname(path) !== ".tsx" || file === "i18n/resources.ts") continue;
    const text = readFileSync(path, "utf8");
    const sourceFile = ts.createSourceFile(path, text, ts.ScriptTarget.Latest, true,
      extname(path) === ".tsx" ? ts.ScriptKind.TSX : ts.ScriptKind.TS);
    function visit(node) {
      if (ts.isJsxText(node) && cjk.test(node.text.trim())) {
        const { line } = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile));
        violations.push(`${file}:${line + 1}: raw JSX ${JSON.stringify(node.text.trim())}`);
      }
      if (ts.isStringLiteralLike(node) && cjk.test(node.text)
          && !isLocalizedLiteral(node, sourceFile)) {
        const { line } = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile));
        violations.push(`${file}:${line + 1}: ${JSON.stringify(node.text)}`);
      }
      ts.forEachChild(node, visit);
    }
    visit(sourceFile);
  }
  assert.deepEqual(violations, [], violations.join("\n"));
});

test("system status surfaces do not render raw backend messages", () => {
  const checks = [
    ["features/library/LibraryPage.tsx", /job\.message/u],
    ["features/grill/GrillPage.tsx", /selection\??\.message/u],
    ["features/timeline/TimelinePage.tsx", /detailPrepError|prepErrorText/u],
    ["features/operations/TrustedImmediateOperationsPanel.tsx", /commandStatus\.error\?\.message/u],
    ["features/chat/ChatPage.tsx", /setError\(\{ message: r\.message/u],
  ];
  for (const [file, pattern] of checks) {
    assert.doesNotMatch(readFileSync(join(srcRoot, file), "utf8"), pattern, file);
  }
});

test("thrown frontend errors are English diagnostics or localized copy", () => {
  const violations = [];
  for (const path of sourceFiles(srcRoot).filter((file) => [".ts", ".tsx"].includes(extname(file)))) {
    const file = relative(srcRoot, path);
    const text = readFileSync(path, "utf8");
    const sourceFile = ts.createSourceFile(
      path,
      text,
      ts.ScriptTarget.Latest,
      true,
      extname(path) === ".tsx" ? ts.ScriptKind.TSX : ts.ScriptKind.TS,
    );
    function visit(node) {
      if (ts.isNewExpression(node) && node.expression.getText(sourceFile) === "Error") {
        const values = stringValues(node);
        if (values.some((value) => cjk.test(value)) && !hasNativePair(node)) {
          const { line } = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile));
          violations.push(`${file}:${line + 1}`);
        }
      }
      ts.forEachChild(node, visit);
    }
    visit(sourceFile);
  }
  assert.deepEqual(violations, [], violations.join("\n"));
});

test("frontend code comments are English-only", () => {
  const violations = [];
  for (const path of frontendCommentFiles().filter((file) => extname(file) !== ".css")) {
    const file = relative(frontendRoot, path);
    const text = readFileSync(path, "utf8");
    const scanner = ts.createScanner(
      ts.ScriptTarget.Latest,
      false,
      ts.LanguageVariant.Standard,
      text,
    );
    for (let token = scanner.scan(); token !== ts.SyntaxKind.EndOfFileToken; token = scanner.scan()) {
      if ((token === ts.SyntaxKind.SingleLineCommentTrivia
          || token === ts.SyntaxKind.MultiLineCommentTrivia)
          && cjk.test(scanner.getTokenText())) {
        const line = text.slice(0, scanner.getTokenPos()).split("\n").length;
        violations.push(`${file}:${line}`);
      }
    }
  }
  assert.deepEqual(violations, [], violations.join("\n"));
});

test("CSS production comments are English-only", () => {
  const violations = [];
  for (const path of frontendCommentFiles().filter((file) => extname(file) === ".css")) {
    const text = readFileSync(path, "utf8");
    for (const match of text.matchAll(/\/\*[\s\S]*?\*\//gu)) {
      if (!cjk.test(match[0])) continue;
      const line = text.slice(0, match.index).split("\n").length;
      violations.push(`${relative(frontendRoot, path)}:${line}`);
    }
  }
  assert.deepEqual(violations, [], violations.join("\n"));
});

test("HTML comments are English-only", () => {
  const path = join(frontendRoot, "index.html");
  const text = readFileSync(path, "utf8");
  const violations = [...text.matchAll(/<!--[\s\S]*?-->/gu)]
    .filter((match) => cjk.test(match[0]))
    .map((match) => `index.html:${text.slice(0, match.index).split("\n").length}`);
  assert.deepEqual(violations, [], violations.join("\n"));
});
