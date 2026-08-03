import assert from "node:assert/strict";
import { access } from "node:fs/promises";
import path from "node:path";
import { test } from "node:test";

import ts from "typescript";

import {
  assertNoOwnerForwarding,
  descendants,
  exportedNames,
  importsFrom,
  isWithin,
  moduleReferences,
  moduleStem,
  recordFor,
  resolveLocalModule,
  runtimeDefinitionNames,
  sourceRecords,
  sourceRootPath,
} from "../../test-support/typescriptArchitecture.mjs";

const sharedApiPath = path.join(sourceRootPath, "shared", "api");
const rootApiPath = path.join(sourceRootPath, "api.ts");
const transportPath = path.join(sharedApiPath, "transport.ts");
const headersPath = path.join(sharedApiPath, "headers.ts");
const requestTimeoutPath = path.join(sharedApiPath, "requestTimeout.ts");
const runtimeLocalePath = path.join(sharedApiPath, "runtimeLocale.ts");
const operationRequestsPath = path.join(sharedApiPath, "operationRequests.ts");
const forbiddenHeadersPath = path.join(sourceRootPath, "requestHeaders.ts");
const preferenceOperationApiPath = path.join(
  sourceRootPath,
  "features",
  "preference-operations",
  "preferenceOperationApi.ts",
);
const preferencesApiPath = path.join(
  sourceRootPath,
  "features",
  "preferences",
  "preferencesApi.ts",
);
const intakeOperationApiPath = path.join(
  sourceRootPath,
  "features",
  "intake-operations",
  "intakeOperationApi.ts",
);
const chatApiPath = path.join(
  sourceRootPath,
  "features",
  "chat",
  "chatApi.ts",
);
const applicationDeleteApiPath = path.join(
  sourceRootPath,
  "features",
  "application-delete-operations",
  "applicationDeleteOperationApi.ts",
);
const applicationMergeApiPath = path.join(
  sourceRootPath,
  "features",
  "application-merge-operations",
  "applicationMergeOperationApi.ts",
);
const applicationUpdateApiPath = path.join(
  sourceRootPath,
  "features",
  "application-update-operations",
  "applicationUpdateOperationApi.ts",
);
const reviewTimelineEntryEditApiPath = path.join(
  sourceRootPath,
  "features",
  "review-timeline-entry-edit-operations",
  "reviewTimelineEntryEditOperationApi.ts",
);
const reviewRecordApiPath = path.join(
  sourceRootPath,
  "features",
  "review-record-operations",
  "reviewRecordOperationApi.ts",
);
const reviewUndoApiPath = path.join(
  sourceRootPath,
  "features",
  "review-operations",
  "reviewUndoOperationApi.ts",
);

const HEADER_EXPORTS = ["WRITE_HEADERS"];
const REQUEST_TIMEOUT_EXPORTS = ["withRequestTimeout"];
const TRANSPORT_EXPORTS = [
  "HttpError",
  "del",
  "getJson",
  "postForm",
  "postJson",
  "putForm",
  "putJson",
];
const OPERATION_REQUEST_EXPORTS = [
  "OPERATION_TIMEOUT_MESSAGE",
  "OperationRequestOptions",
  "decideOperation",
  "getOperation",
  "getOperationCommandStatus",
  "listOperationsAt",
  "listOperationsByClientTurn",
  "listPendingOperations",
  "postOperationCommand",
  "postOperationList",
  "postOperationUndoCommand",
  "postOperationForm",
];
const MOVED_EXPORTS = new Set([...HEADER_EXPORTS, ...TRANSPORT_EXPORTS]);
const SHARED_API_EXPORTS = new Set([
  ...MOVED_EXPORTS,
  ...REQUEST_TIMEOUT_EXPORTS,
  ...OPERATION_REQUEST_EXPORTS,
]);
const FORBIDDEN_TIMEOUT_NAME = "withTrustedOperationTimeout";
const TRUSTED_OPERATION_TIMEOUT_MESSAGE = {
  zhCN: "响应超时，系统会继续核对这次操作的最终结果。",
  en: "The response timed out. CareerDesk will keep checking the final outcome.",
};
const PREFERENCE_TIMEOUT_MESSAGES = [
  { zhCN: "长期偏好读取超时，请重试。", en: "Loading long-term preferences timed out. Try again." },
  { zhCN: "偏好修改状态读取超时，请继续核对。", en: "Checking the preference update timed out. Keep checking its final state." },
  { zhCN: "偏好修改响应超时；结果仍待核对。", en: "The preference update timed out; its final state still needs verification." },
  { zhCN: "安全停止请求超时；结果仍待核对。", en: "The safe-stop request timed out; its final state still needs verification." },
];
const INTAKE_TIMEOUT_MESSAGES = new Map([
  ["INTAKE_LIST_TIMEOUT_MESSAGE", { zhCN: "待确认岗位列表读取超时，请重试。", en: "Loading pending role imports timed out. Try again." }],
  ["INTAKE_STATUS_TIMEOUT_MESSAGE", { zhCN: "岗位变更状态读取超时，请继续核对。", en: "Checking the role import timed out. Keep checking its final state." }],
  ["INTAKE_COMMAND_TIMEOUT_MESSAGE", { zhCN: "岗位变更操作响应超时；结果仍待核对。", en: "The role import action timed out; its final state still needs verification." }],
]);

function callsNamed(sourceFile, name) {
  return descendants(
    sourceFile,
    (node) => ts.isCallExpression(node)
      && ts.isIdentifier(node.expression)
      && node.expression.text === name,
  );
}

function compactNodeText(node, sourceFile) {
  return node.getText(sourceFile).replace(/\s+/g, "");
}

function unwrapExpression(node) {
  let current = node;
  while (ts.isAsExpression(current) || ts.isSatisfiesExpression(current)
      || ts.isParenthesizedExpression(current)) current = current.expression;
  return current;
}

function localizedObjectValue(node) {
  const object = unwrapExpression(node);
  assert.ok(ts.isObjectLiteralExpression(object), "localized copy must be a static object");
  return Object.fromEntries(object.properties.map((property) => {
    assert.ok(ts.isPropertyAssignment(property) && ts.isIdentifier(property.name));
    assert.ok(ts.isStringLiteralLike(property.initializer));
    return [property.name.text, property.initializer.text];
  }));
}

function localizedConstantValue(sourceFile, name) {
  const declarations = descendants(
    sourceFile,
    (node) => ts.isVariableDeclaration(node)
      && ts.isIdentifier(node.name)
      && node.name.text === name,
  );
  assert.equal(declarations.length, 1, `${name} must have one declaration`);
  assert.ok(declarations[0].initializer);
  return localizedObjectValue(declarations[0].initializer);
}

test("shared API utilities have one exact public owner", async () => {
  const records = await sourceRecords();
  const headers = recordFor(records, headersPath);
  const transport = recordFor(records, transportPath);
  const requestTimeout = recordFor(records, requestTimeoutPath);

  assert.deepEqual(exportedNames(headers.sourceFile).sort(), HEADER_EXPORTS);
  assert.deepEqual(exportedNames(transport.sourceFile).sort(), TRANSPORT_EXPORTS.slice().sort());
  assert.deepEqual(exportedNames(requestTimeout.sourceFile), REQUEST_TIMEOUT_EXPORTS);
  for (const owner of [headers, requestTimeout, transport]) {
    const defaultSurfaces = owner.sourceFile.statements.filter((statement) => (
      ts.isExportAssignment(statement)
      || (ts.canHaveModifiers(statement) && ts.getModifiers(statement)?.some(
        (modifier) => modifier.kind === ts.SyntaxKind.DefaultKeyword,
      ))
    ));
    assert.equal(
      defaultSurfaces.length,
      0,
      `${path.relative(sourceRootPath, owner.filename)} must not expose a default surface`,
    );
  }

  const operationRequests = recordFor(records, operationRequestsPath);
  assert.deepEqual(
    exportedNames(operationRequests.sourceFile).sort(),
    OPERATION_REQUEST_EXPORTS.slice().sort(),
  );
  for (const symbol of SHARED_API_EXPORTS) {
    const owners = records
      .filter(({ sourceFile }) => exportedNames(sourceFile).includes(symbol))
      .map(({ filename }) => path.relative(sourceRootPath, filename))
      .sort();
    const expected = symbol === "WRITE_HEADERS"
      ? ["shared/api/headers.ts"]
      : symbol === "withRequestTimeout"
        ? ["shared/api/requestTimeout.ts"]
        : OPERATION_REQUEST_EXPORTS.includes(symbol)
          ? ["shared/api/operationRequests.ts"]
          : ["shared/api/transport.ts"];
    assert.deepEqual(owners, expected, `${symbol} must have one public owner`);
  }

  for (const [symbol, expectedOwner] of [
    ["HttpError", "shared/api/transport.ts"],
    ["WRITE_HEADERS", "shared/api/headers.ts"],
    ["withRequestTimeout", "shared/api/requestTimeout.ts"],
  ]) {
    const definitions = records
      .filter(({ sourceFile }) => runtimeDefinitionNames(sourceFile).includes(symbol))
      .map(({ filename }) => path.relative(sourceRootPath, filename))
      .sort();
    assert.deepEqual(
      definitions,
      [expectedOwner],
      `${symbol} must have one runtime definition, including private declarations`,
    );
  }

  assertNoOwnerForwarding(
    records,
    [headersPath, requestTimeoutPath, transportPath, operationRequestsPath],
    "an API owner",
  );

  await assert.rejects(
    access(forbiddenHeadersPath),
    (error) => error?.code === "ENOENT",
    "requestHeaders.ts must stay absent",
  );
});

test("the root domain API stays absent without a compatibility shim", async () => {
  const records = await sourceRecords();
  const rootApiStem = moduleStem(rootApiPath);

  for (const { filename, sourceFile } of records) {
    for (const { specifier } of moduleReferences(sourceFile)) {
      const target = resolveLocalModule(filename, specifier);
      assert.notEqual(
        target,
        rootApiStem,
        `${path.relative(sourceRootPath, filename)} still depends on removed api.ts`,
      );
    }
  }
  await assert.rejects(
    access(rootApiPath),
    (error) => error?.code === "ENOENT",
    "root api.ts must stay absent",
  );
});

test("request timeout policy is builder-owned with caller-owned messages", async () => {
  const records = await sourceRecords();
  const requestTimeout = recordFor(records, requestTimeoutPath);
  const operationRequests = recordFor(records, operationRequestsPath);
  const preferenceOperationApi = recordFor(records, preferenceOperationApiPath);
  const preferencesApi = recordFor(records, preferencesApiPath);
  const intakeOperationApi = recordFor(records, intakeOperationApiPath);
  const chatApi = recordFor(records, chatApiPath);
  const operationApiOwners = [
    [applicationDeleteApiPath, ["decideOperation", "getOperation", "listPendingOperations", "postOperationCommand"]],
    [applicationMergeApiPath, ["decideOperation", "getOperation", "listPendingOperations"]],
    [applicationUpdateApiPath, ["getOperation", "getOperationCommandStatus", "listOperationsByClientTurn", "postOperationUndoCommand"]],
    [reviewTimelineEntryEditApiPath, ["getOperation", "getOperationCommandStatus", "listOperationsByClientTurn", "postOperationUndoCommand"]],
    [reviewRecordApiPath, ["decideOperation", "getOperation", "listOperationsAt", "listOperationsByClientTurn", "postOperationCommand", "postOperationList"]],
    [reviewUndoApiPath, ["decideOperation", "getOperation", "listPendingOperations", "postOperationCommand"]],
    [intakeOperationApiPath, ["decideOperation", "getOperation", "listPendingOperations", "postOperationForm"]],
  ];

  const timeoutDeclarations = requestTimeout.sourceFile.statements.filter(
    (statement) => ts.isFunctionDeclaration(statement)
      && statement.name?.text === "withRequestTimeout",
  );
  assert.equal(timeoutDeclarations.length, 1);
  const [timeoutDeclaration] = timeoutDeclarations;
  assert.deepEqual(
    timeoutDeclaration.parameters.map((parameter) => (
      ts.isIdentifier(parameter.name) ? parameter.name.text : null
    )),
    ["request", "externalSignal", "timeoutMessage"],
  );
  for (const parameter of timeoutDeclaration.parameters) {
    assert.equal(parameter.questionToken, undefined, `${parameter.name.getText()} must be required`);
    assert.equal(parameter.initializer, undefined, `${parameter.name.getText()} must have no default`);
  }
  const externalSignalType = timeoutDeclaration.parameters[1].type;
  assert.ok(externalSignalType && ts.isUnionTypeNode(externalSignalType));
  assert.deepEqual(
    externalSignalType.types
      .map((type) => compactNodeText(type, requestTimeout.sourceFile))
      .sort(),
    ["AbortSignal", "null", "undefined"].sort(),
    "externalSignal must be required while explicitly accepting null and undefined",
  );
  assert.equal(
    compactNodeText(timeoutDeclaration.parameters[2].type, requestTimeout.sourceFile),
    "string|LocalizedRuntimeMessage",
    "timeoutMessage must accept static or localized caller-owned copy",
  );

  const deadlineDeclarations = descendants(
    requestTimeout.sourceFile,
    (node) => ts.isVariableDeclaration(node)
      && ts.isIdentifier(node.name)
      && node.name.text === "REQUEST_TIMEOUT_MS",
  );
  assert.equal(deadlineDeclarations.length, 1);
  const deadline = deadlineDeclarations[0].initializer;
  assert.ok(deadline && ts.isNumericLiteral(deadline));
  assert.equal(Number(deadline.getText(requestTimeout.sourceFile).replaceAll("_", "")), 12_000);

  const timeoutErrors = descendants(
    timeoutDeclaration.body,
    (node) => ts.isNewExpression(node)
      && ts.isIdentifier(node.expression)
      && node.expression.text === "Error",
  );
  assert.equal(timeoutErrors.length, 1, "timeout policy must construct one plain Error");
  assert.deepEqual(
    timeoutErrors[0].arguments?.map((argument) => compactNodeText(
      argument,
      requestTimeout.sourceFile,
    )),
    ["localizeRuntimeMessage(timeoutMessage)"],
    "the timeout Error must resolve caller-owned copy in the current runtime locale",
  );

  const forbiddenLocations = records.flatMap(({ filename, sourceFile }) => descendants(
    sourceFile,
    (node) => ts.isIdentifier(node) && node.text === FORBIDDEN_TIMEOUT_NAME,
  ).map(() => path.relative(sourceRootPath, filename)));
  assert.deepEqual(
    forbiddenLocations,
    [],
    `${FORBIDDEN_TIMEOUT_NAME} must have no definition, import, export, or call`,
  );

  // Keep timeout ownership limited to the shared builders and three bespoke API modules.
  const timeoutOwnerStem = moduleStem(requestTimeoutPath);
  const ownerReferences = records.flatMap(({ filename, sourceFile }) => moduleReferences(sourceFile)
    .filter(({ specifier }) => resolveLocalModule(filename, specifier) === timeoutOwnerStem)
    .map(({ node }) => ({ filename, node })));
  assert.deepEqual(
    ownerReferences.map(({ filename }) => path.relative(sourceRootPath, filename)).sort(),
    [
      operationRequestsPath,
      preferenceOperationApiPath,
      preferencesApiPath,
      chatApiPath,
    ]
      .map((filename) => path.relative(sourceRootPath, filename))
      .sort(),
    "only the shared operation builders and the three bespoke owners may import requestTimeout.ts",
  );

  const expectedCallCounts = new Map([
    [operationRequestsPath, 10],
    [preferenceOperationApiPath, 1],
    [preferencesApiPath, 4],
    [chatApiPath, 4],
  ]);
  const actualCallCounts = records
    .map(({ filename, sourceFile }) => [filename, callsNamed(sourceFile, "withRequestTimeout").length])
    .filter(([, count]) => count > 0);
  assert.deepEqual(
    actualCallCounts
      .map(([filename, count]) => [path.relative(sourceRootPath, filename), count])
      .sort(([left], [right]) => left.localeCompare(right)),
    [...expectedCallCounts]
      .map(([filename, count]) => [path.relative(sourceRootPath, filename), count])
      .sort(([left], [right]) => left.localeCompare(right)),
    "raw timeout calls must stay in the builder module and the three bespoke owners",
  );
  for (const [filename] of expectedCallCounts) {
    const record = recordFor(records, filename);
    for (const call of callsNamed(record.sourceFile, "withRequestTimeout")) {
      assert.equal(
        call.arguments.length,
        3,
        `timeout call in ${path.relative(sourceRootPath, filename)} must have three arguments`,
      );
    }
  }

  for (const call of callsNamed(operationRequests.sourceFile, "withRequestTimeout")) {
    assert.equal(
      compactNodeText(call.arguments[1], operationRequests.sourceFile),
      "options?.init?.signal",
    );
    assert.equal(
      compactNodeText(call.arguments[2], operationRequests.sourceFile),
      "messageFor(options)",
    );
  }
  assert.deepEqual(
    localizedConstantValue(operationRequests.sourceFile, "OPERATION_TIMEOUT_MESSAGE"),
    TRUSTED_OPERATION_TIMEOUT_MESSAGE,
  );

  // The seven operation APIs use only shared request builders and remain sole endpoint owners.
  const builderStem = moduleStem(operationRequestsPath);
  const transportStem = moduleStem(transportPath);
  for (const [filename, expectedBuilders] of operationApiOwners) {
    const record = recordFor(records, filename);
    const references = moduleReferences(record.sourceFile).map(({ specifier }) => (
      resolveLocalModule(filename, specifier)
    ));
    assert.ok(
      references.includes(builderStem),
      `${path.relative(sourceRootPath, filename)} must use the shared operation builders`,
    );
    assert.ok(
      !references.includes(transportStem) && !references.includes(timeoutOwnerStem),
      `${path.relative(sourceRootPath, filename)} must not bypass the shared builders`,
    );
    const builderImports = importsFrom(record.sourceFile, filename, operationRequestsPath)
      .flatMap((statement) => {
        const bindings = statement.importClause?.namedBindings;
        assert.ok(bindings && ts.isNamedImports(bindings));
        return bindings.elements.map((element) => element.propertyName?.text ?? element.name.text);
      });
    assert.deepEqual(
      builderImports.sort(),
      expectedBuilders.slice().sort(),
      `${path.relative(sourceRootPath, filename)} must import exactly its builder set`,
    );
  }

  assert.deepEqual(
    localizedConstantValue(chatApi.sourceFile, "CHAT_TIMEOUT_MESSAGE"),
    TRUSTED_OPERATION_TIMEOUT_MESSAGE,
  );
  assert.deepEqual(
    localizedConstantValue(
      preferenceOperationApi.sourceFile,
      "PREFERENCE_OPERATION_TIMEOUT_MESSAGE",
    ),
    TRUSTED_OPERATION_TIMEOUT_MESSAGE,
  );
  assert.equal(
    exportedNames(preferenceOperationApi.sourceFile)
      .includes("PREFERENCE_OPERATION_TIMEOUT_MESSAGE"),
    false,
  );
  for (const [name, message] of INTAKE_TIMEOUT_MESSAGES) {
    assert.deepEqual(localizedConstantValue(intakeOperationApi.sourceFile, name), message);
    assert.equal(exportedNames(intakeOperationApi.sourceFile).includes(name), false);
  }

  for (const call of callsNamed(chatApi.sourceFile, "withRequestTimeout")) {
    assert.ok(
      ts.isIdentifier(call.arguments[2])
        && call.arguments[2].text === "CHAT_TIMEOUT_MESSAGE",
      "all three Chat calls must use the Chat-owned message",
    );
    assert.equal(compactNodeText(call.arguments[1], chatApi.sourceFile), "init?.signal");
  }
  for (const call of callsNamed(preferenceOperationApi.sourceFile, "withRequestTimeout")) {
    assert.ok(
      ts.isIdentifier(call.arguments[2])
        && call.arguments[2].text === "PREFERENCE_OPERATION_TIMEOUT_MESSAGE",
      "the preference-operation call must use its feature message owner",
    );
    assert.equal(
      compactNodeText(call.arguments[1], preferenceOperationApi.sourceFile),
      "init?.signal",
    );
  }
  assert.deepEqual(
    callsNamed(preferencesApi.sourceFile, "withRequestTimeout").map((call) => {
      const message = call.arguments[2];
      assert.equal(compactNodeText(call.arguments[1], preferencesApi.sourceFile), "init?.signal");
      return localizedObjectValue(message);
    }),
    PREFERENCE_TIMEOUT_MESSAGES,
    "the four preferences calls must keep their feature-specific messages",
  );
});

test("shared API is downward-only and contains no domain endpoint", async () => {
  const records = await sourceRecords();
  const sharedRecords = records.filter(({ filename }) => isWithin(filename, sharedApiPath));
  const headers = recordFor(records, headersPath);
  const requestTimeout = recordFor(records, requestTimeoutPath);

  assert.equal(moduleReferences(headers.sourceFile).length, 0, "headers.ts must stay a leaf");
  assert.deepEqual(
    moduleReferences(requestTimeout.sourceFile).map(({ specifier }) => (
      resolveLocalModule(requestTimeoutPath, specifier)
    )),
    [moduleStem(runtimeLocalePath)],
    "requestTimeout.ts may depend only on the locale-neutral runtime selector",
  );

  for (const { filename, sourceFile } of sharedRecords) {
    for (const { specifier } of moduleReferences(sourceFile)) {
      const target = resolveLocalModule(filename, specifier);
      if (target !== null) {
        assert.ok(
          isWithin(target, sharedApiPath),
          `${path.relative(sourceRootPath, filename)} imports upward to ${specifier}`,
        );
      }
    }

    const domainUrls = descendants(
      sourceFile,
      (node) => (ts.isStringLiteralLike(node) && node.text.startsWith("/api/"))
        || (ts.isTemplateExpression(node) && node.head.text.startsWith("/api/")),
    );
    assert.equal(
      domainUrls.length,
      0,
      `${path.relative(sourceRootPath, filename)} must not own domain URLs`,
    );
  }
});

test("production local imports remain acyclic", async () => {
  const records = await sourceRecords();
  const recordByStem = new Map(records.map((record) => [moduleStem(record.filename), record]));
  function graphTarget(filename, specifier) {
    const target = resolveLocalModule(filename, specifier);
    if (target === null) return null;
    if (recordByStem.has(target)) return target;
    const indexTarget = path.join(target, "index");
    return recordByStem.has(indexTarget) ? indexTarget : target;
  }
  const edges = new Map(records.map(({ filename, sourceFile }) => [
    moduleStem(filename),
    moduleReferences(sourceFile)
      .map(({ specifier }) => graphTarget(filename, specifier))
      .filter((target) => target !== null && recordByStem.has(target)),
  ]));
  const state = new Map();
  const stack = [];

  function visit(module) {
    if (state.get(module) === 2) return;
    if (state.get(module) === 1) {
      const cycleStart = stack.indexOf(module);
      const cycle = [...stack.slice(cycleStart), module]
        .map((item) => path.relative(sourceRootPath, recordByStem.get(item).filename));
      assert.fail(`production import cycle: ${cycle.join(" -> ")}`);
    }
    state.set(module, 1);
    stack.push(module);
    for (const dependency of edges.get(module) ?? []) visit(dependency);
    stack.pop();
    state.set(module, 2);
  }

  for (const module of recordByStem.keys()) visit(module);
});
