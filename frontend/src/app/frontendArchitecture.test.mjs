import assert from "node:assert/strict";
import { access, readFile, readdir } from "node:fs/promises";
import { test } from "node:test";
import ts from "typescript";

const appUrl = new URL("./App.tsx", import.meta.url);
const mainUrl = new URL("../main.tsx", import.meta.url);
const routeContentUrl = new URL("./RouteContent.tsx", import.meta.url);
const routeErrorBoundaryUrl = new URL("./RouteErrorBoundary.tsx", import.meta.url);
const routePathsUrl = new URL("./routePaths.ts", import.meta.url);
const sourceRootUrl = new URL("../", import.meta.url);
const viteConfigUrl = new URL("../../vite.config.ts", import.meta.url);

const pageOwners = [
  ["chat", "ChatPage"],
  ["grill", "GrillLabPage"],
  ["grill", "GrillPage"],
  ["library", "LibraryPage"],
  ["questions", "QuestionsPage"],
  ["settings", "SettingsPage"],
  ["timeline", "TimelinePage"],
];
const lazyRouteFeatures = [
  ["grill", "GrillLabPage"],
  ["library", "LibraryPage"],
  ["settings", "SettingsPage"],
  ["timeline", "TimelinePage"],
];

async function assertMissing(url) {
  await assert.rejects(
    access(url),
    (error) => error?.code === "ENOENT",
    url.pathname,
  );
}

async function pageOwnerPaths(directory, prefix = "") {
  const entries = await readdir(directory, { withFileTypes: true });
  const paths = [];
  for (const entry of entries) {
    const relativePath = `${prefix}${entry.name}`;
    if (entry.isDirectory()) {
      paths.push(...await pageOwnerPaths(
        new URL(`${entry.name}/`, directory),
        `${relativePath}/`,
      ));
    } else if (/Page\.tsx$/.test(entry.name)) {
      paths.push(relativePath);
    }
  }
  return paths;
}

function descendants(root, predicate) {
  const matches = [];
  function visit(node) {
    if (predicate(node)) matches.push(node);
    ts.forEachChild(node, visit);
  }
  visit(root);
  return matches;
}

function unwrapParentheses(node) {
  let current = node;
  while (ts.isParenthesizedExpression(current)) current = current.expression;
  return current;
}

function declarationInitializer(sourceFile, name) {
  const declarations = descendants(
    sourceFile,
    (node) => ts.isVariableDeclaration(node)
      && ts.isIdentifier(node.name)
      && node.name.text === name,
  );
  assert.equal(declarations.length, 1, `${name} must have one declaration`);
  assert.ok(declarations[0].initializer, `${name} must have an initializer`);
  return declarations[0].initializer;
}

function objectPropertyExpressions(node, sourceFile) {
  assert.ok(ts.isObjectLiteralExpression(node));
  return Object.fromEntries(node.properties.map((property) => {
    assert.ok(ts.isPropertyAssignment(property));
    return [
      property.name.getText(sourceFile),
      property.initializer.getText(sourceFile).replace(/\s+/g, ""),
    ];
  }));
}

function jsxTagName(node, sourceFile) {
  if (ts.isJsxElement(node)) return node.openingElement.tagName.getText(sourceFile);
  if (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) {
    return node.tagName.getText(sourceFile);
  }
  return null;
}

function jsxAttributeInitializer(node, name, sourceFile) {
  const attributes = ts.isJsxElement(node)
    ? node.openingElement.attributes
    : node.attributes;
  const attribute = attributes.properties.find(
    (candidate) => ts.isJsxAttribute(candidate)
      && candidate.name.getText(sourceFile) === name,
  );
  assert.ok(attribute, `missing ${name}`);
  assert.ok(attribute.initializer);
  return attribute.initializer;
}

function jsxAttributeExpression(node, name, sourceFile) {
  const initializer = jsxAttributeInitializer(node, name, sourceFile);
  assert.ok(ts.isJsxExpression(initializer));
  assert.ok(initializer.expression);
  return initializer.expression.getText(sourceFile).replace(/\s+/g, "");
}

function jsxAttributeValue(node, name, sourceFile) {
  const initializer = jsxAttributeInitializer(node, name, sourceFile);
  if (ts.isStringLiteral(initializer)) return initializer.text;
  assert.ok(ts.isJsxExpression(initializer));
  assert.ok(initializer.expression);
  return initializer.expression.getText(sourceFile).replace(/\s+/g, "");
}

test("the app shell and route pages have one feature-first owner", async () => {
  const mainSource = await readFile(mainUrl, "utf8");

  assert.match(mainSource, /from\s+["']\.\/app\/App["']/);
  await assertMissing(new URL("App.tsx", sourceRootUrl));
  await assertMissing(new URL("pages/", sourceRootUrl));

  assert.deepEqual(
    (await pageOwnerPaths(sourceRootUrl)).sort(),
    pageOwners.map(([feature, page]) => `features/${feature}/${page}.tsx`).sort(),
  );

  for (const [feature, page] of pageOwners) {
    const pageUrl = new URL(`../features/${feature}/${page}.tsx`, import.meta.url);
    const pageSource = await readFile(pageUrl, "utf8");

    assert.match(
      pageSource,
      new RegExp(`export\\s+function\\s+${page}\\s*\\(`),
      pageUrl.pathname,
    );
    assert.doesNotMatch(
      pageSource,
      /from\s+["'][^"']*\/app(?:\/[^"']*)?["']/,
      `${page} must not depend back on the app composition layer`,
    );
  }
});

test("the desktop shell leaves more horizontal room for the timeline board", async () => {
  const source = await readFile(appUrl, "utf8");
  assert.match(source, /\? "max-w-\[1760px\]"/);
  assert.match(source, /sidebarCollapsed \? "w-16" : "w-52"/);
  assert.match(source, /h-10 w-10 justify-center rounded-\[11px\]/);
  assert.match(source, /top-1\/2 z-20 flex h-7 w-\[18px\] translate-x-1\/2 -translate-y-1\/2/);
  assert.match(source, /aria-label=\{sidebarCollapsed \? t\("shell\.sidebar\.expand"\) : t\("shell\.sidebar\.collapse"\)\}/);
  assert.match(source, /min-width: 768px\) and \(max-width: 1023px/);
  assert.match(source, /window\.localStorage\.setItem\(SIDEBAR_COLLAPSED_KEY/);
});

test("only the four optional route groups are dynamically imported", async () => {
  const [appSource, routeContentSource, boundarySource, viteConfigSource] = await Promise.all([
    readFile(appUrl, "utf8"),
    readFile(routeContentUrl, "utf8"),
    readFile(routeErrorBoundaryUrl, "utf8"),
    readFile(viteConfigUrl, "utf8"),
  ]);
  const appSourceFile = ts.createSourceFile(
    appUrl.pathname,
    appSource,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  );
  const routeContentSourceFile = ts.createSourceFile(
    routeContentUrl.pathname,
    routeContentSource,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  );
  const appImports = appSourceFile.statements
    .filter(ts.isImportDeclaration)
    .map((statement) => statement.moduleSpecifier.text);
  const dynamicImports = descendants(
    routeContentSourceFile,
    (node) => ts.isCallExpression(node)
      && node.expression.kind === ts.SyntaxKind.ImportKeyword,
  );
  const dynamicSpecifiers = dynamicImports.map((node) => {
    assert.equal(node.arguments.length, 1);
    assert.ok(ts.isStringLiteral(node.arguments[0]));
    return node.arguments[0].text;
  });

  assert.ok(appImports.includes("../features/chat/ChatPage"));
  assert.ok(appImports.includes("./RouteContent"));
  assert.deepEqual(
    dynamicSpecifiers.sort(),
    lazyRouteFeatures
      .map(([feature, page]) => `../features/${feature}/${page}`)
      .sort(),
  );
  assert.doesNotMatch(routeContentSource, /ChatPage/);

  for (const [feature, page] of lazyRouteFeatures) {
    const specifier = `../features/${feature}/${page}`;
    assert.ok(!appImports.includes(specifier), `${page} must not be eager`);

    const declarations = routeContentSourceFile.statements
      .filter(ts.isVariableStatement)
      .flatMap((statement) => [...statement.declarationList.declarations])
      .filter((declaration) => declaration.name.getText(routeContentSourceFile) === page);
    assert.equal(declarations.length, 1, `${page} must have one top-level lazy binding`);
    const lazyCall = declarations[0].initializer;
    assert.ok(lazyCall && ts.isCallExpression(lazyCall));
    assert.equal(lazyCall.expression.getText(routeContentSourceFile), "lazy");
    assert.equal(lazyCall.arguments.length, 1);
    const loader = lazyCall.arguments[0];
    assert.ok(ts.isArrowFunction(loader));

    const thenCall = unwrapParentheses(loader.body);
    assert.ok(ts.isCallExpression(thenCall));
    assert.ok(ts.isPropertyAccessExpression(thenCall.expression));
    assert.equal(thenCall.expression.name.text, "then");
    const importCall = thenCall.expression.expression;
    assert.ok(ts.isCallExpression(importCall));
    assert.equal(importCall.expression.kind, ts.SyntaxKind.ImportKeyword);
    assert.equal(importCall.arguments.length, 1);
    assert.ok(ts.isStringLiteral(importCall.arguments[0]));
    assert.equal(importCall.arguments[0].text, specifier);

    assert.equal(thenCall.arguments.length, 1);
    const mapper = thenCall.arguments[0];
    assert.ok(ts.isArrowFunction(mapper));
    assert.equal(mapper.parameters.length, 1);
    assert.ok(ts.isIdentifier(mapper.parameters[0].name));
    const moduleBinding = mapper.parameters[0].name.text;
    const mappedModule = unwrapParentheses(mapper.body);
    assert.ok(ts.isObjectLiteralExpression(mappedModule));
    assert.equal(mappedModule.properties.length, 1);
    const defaultProperty = mappedModule.properties[0];
    assert.ok(ts.isPropertyAssignment(defaultProperty));
    assert.equal(defaultProperty.name.getText(routeContentSourceFile), "default");
    assert.ok(ts.isPropertyAccessExpression(defaultProperty.initializer));
    assert.equal(defaultProperty.initializer.expression.getText(routeContentSourceFile), moduleBinding);
    assert.equal(defaultProperty.initializer.name.text, page);
  }

  for (const dynamicImport of dynamicImports) {
    let ancestor = dynamicImport.parent;
    while (ancestor && !(ts.isCallExpression(ancestor)
      && ancestor.expression.getText(routeContentSourceFile) === "lazy")) {
      ancestor = ancestor.parent;
    }
    assert.ok(ancestor, "every dynamic route import must be owned by React.lazy");
    let statement = ancestor;
    while (statement && !ts.isVariableStatement(statement)) statement = statement.parent;
    assert.ok(statement && statement.parent === routeContentSourceFile);
  }

  assert.doesNotMatch(
    `${appSource}\n${routeContentSource}\n${boundarySource}`,
    /\b(?:prefetch|preload)\b|import\.meta\.glob/,
  );
  assert.doesNotMatch(viteConfigSource, /\bmanualChunks\b/);
});

test("known route variants use one shell identity and a state-preserving replace", async () => {
  const [appSource, routeContentSource, boundarySource, routePathsSource] = await Promise.all([
    readFile(appUrl, "utf8"),
    readFile(routeContentUrl, "utf8"),
    readFile(routeErrorBoundaryUrl, "utf8"),
    readFile(routePathsUrl, "utf8"),
  ]);
  const appSourceFile = ts.createSourceFile(
    appUrl.pathname,
    appSource,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  );

  assert.equal(
    declarationInitializer(appSourceFile, "canonicalPathname")
      .getText(appSourceFile).replace(/\s+/g, ""),
    "canonicalKnownPathname(location.pathname)",
  );
  assert.equal(
    declarationInitializer(appSourceFile, "effectivePathname")
      .getText(appSourceFile).replace(/\s+/g, ""),
    "canonicalPathname??location.pathname",
  );
  assert.equal(
    declarationInitializer(appSourceFile, "isChatRoute")
      .getText(appSourceFile).replace(/\s+/g, ""),
    "effectivePathname===APP_ROUTE_PATHS.chat",
  );
  assert.equal(
    declarationInitializer(appSourceFile, "meta")
      .getText(appSourceFile).replace(/\s+/g, ""),
    "PAGE_META[effectivePathname]",
  );
  assert.equal(
    declarationInitializer(appSourceFile, "mainWidth")
      .getText(appSourceFile).replace(/\s+/g, ""),
    'effectivePathname===APP_ROUTE_PATHS.timeline?"max-w-[1760px]":effectivePathname===APP_ROUTE_PATHS.settings?"max-w-6xl":"max-w-5xl"',
  );

  const modelBanners = descendants(
    appSourceFile,
    (node) => (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node))
      && jsxTagName(node, appSourceFile) === "ModelSetupBanner",
  );
  assert.equal(modelBanners.length, 1);
  assert.equal(
    jsxAttributeExpression(modelBanners[0], "hidden", appSourceFile),
    "effectivePathname===APP_ROUTE_PATHS.settings",
  );

  const navigateCalls = descendants(
    appSourceFile,
    (node) => ts.isCallExpression(node)
      && node.expression.getText(appSourceFile) === "navigate",
  );
  assert.equal(navigateCalls.length, 1);
  assert.equal(navigateCalls[0].arguments.length, 2);
  assert.deepEqual(
    objectPropertyExpressions(navigateCalls[0].arguments[0], appSourceFile),
    {
      pathname: "canonicalPathname",
      search: "location.search",
      hash: "location.hash",
    },
  );
  assert.deepEqual(
    objectPropertyExpressions(navigateCalls[0].arguments[1], appSourceFile),
    {
      replace: "true",
      state: "location.state",
    },
  );

  let canonicalEffect = navigateCalls[0].parent;
  while (canonicalEffect && !(ts.isCallExpression(canonicalEffect)
    && canonicalEffect.expression.getText(appSourceFile) === "useEffect")) {
    canonicalEffect = canonicalEffect.parent;
  }
  assert.ok(canonicalEffect);
  assert.ok(ts.isArrowFunction(canonicalEffect.arguments[0]));
  assert.ok(ts.isBlock(canonicalEffect.arguments[0].body));
  const canonicalGuards = descendants(
    canonicalEffect.arguments[0],
    (node) => ts.isIfStatement(node),
  );
  assert.equal(canonicalGuards.length, 1);
  assert.equal(
    canonicalGuards[0].expression.getText(appSourceFile).replace(/\s+/g, ""),
    "canonicalPathname===null||canonicalPathname===location.pathname",
  );
  assert.ok(ts.isReturnStatement(canonicalGuards[0].thenStatement));
  assert.equal(canonicalEffect.arguments[0].body.statements[0], canonicalGuards[0]);
  let navigateStatement = navigateCalls[0].parent;
  while (navigateStatement.parent !== canonicalEffect.arguments[0].body) {
    navigateStatement = navigateStatement.parent;
  }
  assert.ok(ts.isExpressionStatement(navigateStatement));
  assert.ok(
    canonicalEffect.arguments[0].body.statements.indexOf(navigateStatement) > 0,
    "canonical navigate must be an unconditional statement after the early-return guard",
  );
  assert.ok(ts.isArrayLiteralExpression(canonicalEffect.arguments[1]));
  assert.deepEqual(
    canonicalEffect.arguments[1].elements.map(
      (element) => element.getText(appSourceFile),
    ),
    [
      "canonicalPathname",
      "location.hash",
      "location.pathname",
      "location.search",
      "location.state",
      "navigate",
    ],
  );

  const scrollCalls = descendants(
    appSourceFile,
    (node) => ts.isCallExpression(node)
      && ts.isPropertyAccessExpression(node.expression)
      && node.expression.name.text === "scrollIntoView",
  );
  assert.equal(scrollCalls.length, 1);
  let scrollEffect = scrollCalls[0].parent;
  while (scrollEffect && !(ts.isCallExpression(scrollEffect)
    && scrollEffect.expression.getText(appSourceFile) === "useEffect")) {
    scrollEffect = scrollEffect.parent;
  }
  assert.ok(scrollEffect);
  assert.ok(ts.isArrayLiteralExpression(scrollEffect.arguments[1]));
  assert.deepEqual(
    scrollEffect.arguments[1].elements.map((element) => element.getText(appSourceFile)),
    ["effectivePathname"],
  );

  const knownPathLiterals = new Set(["/", "/grill", "/timeline", "/questions", "/library", "/settings"]);
  for (const [url, source, kind] of [
    [appUrl, appSource, ts.ScriptKind.TSX],
    [routeContentUrl, routeContentSource, ts.ScriptKind.TSX],
    [routeErrorBoundaryUrl, boundarySource, ts.ScriptKind.TSX],
  ]) {
    const sourceFile = ts.createSourceFile(
      url.pathname,
      source,
      ts.ScriptTarget.Latest,
      true,
      kind,
    );
    const duplicatePaths = descendants(
      sourceFile,
      (node) => ts.isStringLiteral(node) && knownPathLiterals.has(node.text),
    );
    assert.equal(duplicatePaths.length, 0, `${url.pathname} must use APP_ROUTE_PATHS`);
  }
  assert.match(routePathsSource, /const APP_ROUTE_PATHS/);
});

test("the stateful chat stays mounted exactly once outside route ownership", async () => {
  const appSource = await readFile(appUrl, "utf8");
  const sourceFile = ts.createSourceFile(
    appUrl.pathname,
    appSource,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  );
  const chatImports = sourceFile.statements.filter(
    (statement) => ts.isImportDeclaration(statement)
      && statement.moduleSpecifier.text === "../features/chat/ChatPage",
  );
  assert.equal(chatImports.length, 1);
  const chatBindings = chatImports[0].importClause?.namedBindings;
  assert.ok(chatBindings && ts.isNamedImports(chatBindings));
  assert.deepEqual(
    chatBindings.elements.map((element) => ({
      imported: element.propertyName?.text ?? element.name.text,
      local: element.name.text,
    })),
    [{ imported: "ChatPage", local: "ChatPage" }],
  );

  const chatElements = descendants(
    sourceFile,
    (node) => (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node))
      && jsxTagName(node, sourceFile) === "ChatPage",
  );
  const chatCreateElements = descendants(
    sourceFile,
    (node) => ts.isCallExpression(node)
      && node.arguments[0]?.getText(sourceFile) === "ChatPage"
      && (node.expression.getText(sourceFile) === "createElement"
        || node.expression.getText(sourceFile).endsWith(".createElement")),
  );
  const routeContentElements = descendants(
    sourceFile,
    (node) => (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node))
      && jsxTagName(node, sourceFile) === "RouteContent",
  );

  assert.equal(chatElements.length, 1);
  assert.equal(chatCreateElements.length, 0);
  assert.equal(routeContentElements.length, 1);
  assert.ok(chatElements[0].pos < routeContentElements[0].pos);
  assert.equal(
    jsxAttributeExpression(chatElements[0], "active", sourceFile),
    "isChatRoute",
  );
  assert.equal(
    jsxAttributeExpression(routeContentElements[0], "pathname", sourceFile),
    "effectivePathname",
  );

  for (const forbiddenTag of ["Routes", "Suspense", "RouteErrorBoundary"]) {
    assert.equal(
      descendants(
        sourceFile,
        (node) => (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node))
          && jsxTagName(node, sourceFile) === forbiddenTag,
      ).length,
      0,
      `${forbiddenTag} must stay behind RouteContent`,
    );
  }

  const wrapper = chatElements[0].parent;
  assert.ok(ts.isJsxElement(wrapper));
  assert.equal(jsxTagName(wrapper, sourceFile), "div");
  assert.equal(
    jsxAttributeExpression(wrapper, "className", sourceFile),
    'isChatRoute?"":"hidden"',
  );
  assert.equal(
    jsxAttributeExpression(wrapper, "aria-hidden", sourceFile),
    "!isChatRoute",
  );

  for (let ancestor = wrapper.parent; ancestor; ancestor = ancestor.parent) {
    assert.ok(
      !["Routes", "Suspense", "RouteErrorBoundary"].includes(
        jsxTagName(ancestor, sourceFile),
      ),
      "Chat must remain outside route loading and error boundaries",
    );
  }
});

test("optional routes have keyed loading and failure isolation", async () => {
  const [routeContentSource, boundarySource] = await Promise.all([
    readFile(routeContentUrl, "utf8"),
    readFile(routeErrorBoundaryUrl, "utf8"),
  ]);
  const sourceFile = ts.createSourceFile(
    routeContentUrl.pathname,
    routeContentSource,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  );
  const boundarySourceFile = ts.createSourceFile(
    routeErrorBoundaryUrl.pathname,
    boundarySource,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.TSX,
  );
  const routesElements = descendants(
    sourceFile,
    (node) => (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node))
      && jsxTagName(node, sourceFile) === "Routes",
  );
  const boundaries = descendants(
    sourceFile,
    (node) => (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node))
      && jsxTagName(node, sourceFile) === "RouteErrorBoundary",
  );
  const suspenseElements = descendants(
    sourceFile,
    (node) => (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node))
      && jsxTagName(node, sourceFile) === "Suspense",
  );
  const routeElements = descendants(
    sourceFile,
    (node) => ts.isJsxSelfClosingElement(node)
      && jsxTagName(node, sourceFile) === "Route",
  );

  assert.equal(routesElements.length, 1);
  assert.equal(boundaries.length, 1);
  assert.equal(suspenseElements.length, 1);
  assert.equal(routeElements.length, 7);
  assert.equal(jsxAttributeExpression(boundaries[0], "key", sourceFile), "pathname");
  assert.equal(
    jsxAttributeExpression(suspenseElements[0], "fallback", sourceFile),
    "<RouteLoadingState/>",
  );
  assert.deepEqual(
    Object.fromEntries(routeElements.map((route) => [
      jsxAttributeValue(route, "path", sourceFile),
      jsxAttributeExpression(route, "element", sourceFile),
    ])),
    {
      "APP_ROUTE_PATHS.chat": "null",
      "APP_ROUTE_PATHS.grill": "<GrillLabPage/>",
      "APP_ROUTE_PATHS.timeline": "<TimelinePage/>",
      "APP_ROUTE_PATHS.questions": "<Navigatereplaceto={`${APP_ROUTE_PATHS.grill}?view=questions`}/>",
      "APP_ROUTE_PATHS.library": "<LibraryPage/>",
      "APP_ROUTE_PATHS.settings": "<SettingsPage/>",
      "*": "<NotFoundPage/>",
    },
  );

  const routeAncestors = [];
  for (let ancestor = routesElements[0].parent; ancestor; ancestor = ancestor.parent) {
    const tagName = jsxTagName(ancestor, sourceFile);
    if (tagName) routeAncestors.push(tagName);
  }
  assert.ok(routeAncestors.indexOf("Suspense") >= 0);
  assert.ok(routeAncestors.indexOf("RouteErrorBoundary") > routeAncestors.indexOf("Suspense"));

  assert.match(routeContentSource, /role="status"/);
  assert.match(routeContentSource, /aria-live="polite"/);
  assert.match(routeContentSource, /aria-atomic="true"/);
  assert.match(boundarySource, /static getDerivedStateFromError\(\)/);
  assert.doesNotMatch(boundarySource, /\b(?:error|message|stack)\s*:/);
  assert.match(boundarySource, /role="alert"/);
  assert.match(boundarySource, /<Link to=\{APP_ROUTE_PATHS\.chat\}/);
  assert.match(boundarySource, /aria-describedby="route-reload-warning"/);
  assert.match(boundarySource, /id="route-reload-warning"/);
  assert.match(boundarySource, /重新加载会丢失尚未发送的草稿、附件/);

  const reloadReferences = descendants(
    boundarySourceFile,
    (node) => ts.isPropertyAccessExpression(node)
      && node.getText(boundarySourceFile) === "window.location.reload",
  );
  assert.equal(reloadReferences.length, 1);
  const reloadCall = reloadReferences[0].parent;
  assert.ok(ts.isCallExpression(reloadCall));
  assert.equal(reloadCall.expression, reloadReferences[0]);
  let reloadHandler = reloadCall.parent;
  while (reloadHandler && !ts.isJsxAttribute(reloadHandler)) reloadHandler = reloadHandler.parent;
  assert.ok(reloadHandler);
  assert.equal(reloadHandler.name.getText(boundarySourceFile), "onClick");
  let reloadButton = reloadHandler.parent;
  while (reloadButton && !ts.isJsxOpeningElement(reloadButton)) reloadButton = reloadButton.parent;
  assert.ok(reloadButton);
  assert.equal(reloadButton.tagName.getText(boundarySourceFile), "button");
});
