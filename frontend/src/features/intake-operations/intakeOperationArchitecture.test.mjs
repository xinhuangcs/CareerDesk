import assert from "node:assert/strict";
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
  typeDefinitionNames,
} from "../../test-support/typescriptArchitecture.mjs";

const rootApiPath = path.join(sourceRootPath, "api.ts");
const transportPath = path.join(sourceRootPath, "shared", "api", "transport.ts");
const requestTimeoutPath = path.join(sourceRootPath, "shared", "api", "requestTimeout.ts");
const operationRequestsPath = path.join(sourceRootPath, "shared", "api", "operationRequests.ts");
const applicationContractPath = path.join(
  sourceRootPath,
  "features",
  "applications",
  "applicationContract.ts",
);
const intakeFeaturePath = path.join(sourceRootPath, "features", "intake-operations");
const intakeOperationContractPath = path.join(intakeFeaturePath, "intakeOperationContract.ts");
const intakeOperationApiPath = path.join(intakeFeaturePath, "intakeOperationApi.ts");
const intakeOperationRefreshPath = path.join(intakeFeaturePath, "intakeOperationRefresh.ts");
const intakeOperationsPanelPath = path.join(intakeFeaturePath, "IntakeOperationsPanel.tsx");
const timelineWorkbookDialogPath = path.join(
  sourceRootPath,
  "features",
  "timeline",
  "TimelineWorkbookImportDialog.tsx",
);
const chatPagePath = path.join(sourceRootPath, "features", "chat", "ChatPage.tsx");

const APPLICATION_STAGE_VALUES = [
  "applied",
  "backlog",
  "interviewing",
  "offer",
  "pooled",
  "rejected",
  "withdrawn",
  "written_test",
];
const APPLICATION_CONTRACT_EXPORTS = [
  "ApplicationNextAction",
  "ApplicationPriority",
  "ApplicationStage",
  "ApplicationTimelineEntry",
];
const INTAKE_CONTRACT_EXPORTS = ["IntakeOperation", "IntakeOperationState", "IntakePosition"];
const INTAKE_API_EXPORTS = [
  "approveIntakeOperation",
  "getIntakeOperation",
  "getPendingIntakeOperations",
  "rejectIntakeOperation",
  "uploadWorkbookIntake",
];
const INTAKE_REFRESH_EXPORTS = [
  "mergePendingIntakeOperations",
  "reconcileIntakeExcludedRows",
  "retainIntakeOperationErrors",
];

function callsNamed(sourceFile, name) {
  return descendants(
    sourceFile,
    (node) => ts.isCallExpression(node)
      && ts.isIdentifier(node.expression)
      && node.expression.text === name,
  );
}

function compactNodeText(node, sourceFile) {
  return node.getText(sourceFile).replace(/\s+/g, "").replace(/,}/g, "}");
}

test("application timeline and intake operations have feature-owned contracts and API", async () => {
  const records = await sourceRecords();
  const applicationContract = recordFor(records, applicationContractPath);
  const intakeContract = recordFor(records, intakeOperationContractPath);
  const intakeApi = recordFor(records, intakeOperationApiPath);
  const intakeRefresh = recordFor(records, intakeOperationRefreshPath);
  const intakePanel = recordFor(records, intakeOperationsPanelPath);
  assert.match(intakePanel.sourceFile.text, /仅展示将保存的内容/);
  assert.doesNotMatch(intakePanel.sourceFile.text, /未填写|未提取|未提供/);

  assert.deepEqual(
    exportedNames(applicationContract.sourceFile).sort(),
    APPLICATION_CONTRACT_EXPORTS.slice().sort(),
  );
  assert.deepEqual(
    exportedNames(intakeContract.sourceFile).sort(),
    INTAKE_CONTRACT_EXPORTS.slice().sort(),
  );
  assert.deepEqual(
    exportedNames(intakeApi.sourceFile).sort(),
    INTAKE_API_EXPORTS.slice().sort(),
  );
  assert.deepEqual(
    exportedNames(intakeRefresh.sourceFile).sort(),
    INTAKE_REFRESH_EXPORTS.slice().sort(),
  );
  for (const owner of [applicationContract, intakeContract, intakeApi, intakeRefresh]) {
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

  const applicationStageDeclarations = applicationContract.sourceFile.statements.filter(
    (statement) => ts.isTypeAliasDeclaration(statement)
      && statement.name.text === "ApplicationStage",
  );
  assert.equal(applicationStageDeclarations.length, 1);
  const applicationStageType = applicationStageDeclarations[0].type;
  assert.ok(ts.isUnionTypeNode(applicationStageType));
  assert.deepEqual(
    applicationStageType.types.map((type) => {
      assert.ok(ts.isLiteralTypeNode(type) && ts.isStringLiteral(type.literal));
      return type.literal.text;
    }).sort(),
    APPLICATION_STAGE_VALUES,
  );

  const publicOwners = new Map([
    ...APPLICATION_CONTRACT_EXPORTS.map((symbol) => [symbol, applicationContractPath]),
    ...INTAKE_CONTRACT_EXPORTS.map((symbol) => [symbol, intakeOperationContractPath]),
    ...INTAKE_API_EXPORTS.map((symbol) => [symbol, intakeOperationApiPath]),
  ]);
  for (const [symbol, expectedOwner] of publicOwners) {
    const owners = records
      .filter(({ sourceFile }) => exportedNames(sourceFile).includes(symbol))
      .map(({ filename }) => filename);
    assert.deepEqual(
      owners,
      symbol === "ApplicationStage"
        ? [expectedOwner, path.join(sourceRootPath, "features", "timeline", "timelineContract.ts")]
        : [expectedOwner],
      `${symbol} must have the expected feature owner`,
    );
  }
  for (const [symbol, expectedOwner] of [
    ...APPLICATION_CONTRACT_EXPORTS.map((symbol) => [symbol, applicationContractPath]),
    ...INTAKE_CONTRACT_EXPORTS.map((symbol) => [symbol, intakeOperationContractPath]),
  ]) {
    const definitions = records
      .filter(({ sourceFile }) => typeDefinitionNames(sourceFile).includes(symbol))
      .map(({ filename }) => filename);
    assert.deepEqual(definitions, [expectedOwner], `${symbol} must have one type definition`);
  }
  for (const symbol of INTAKE_API_EXPORTS) {
    const definitions = records
      .filter(({ sourceFile }) => runtimeDefinitionNames(sourceFile).includes(symbol))
      .map(({ filename }) => filename);
    assert.deepEqual(definitions, [intakeOperationApiPath], `${symbol} must have one runtime owner`);
  }

  const forbiddenStageAliases = records.flatMap(({ filename, sourceFile }) => descendants(
    sourceFile,
    (node) => ts.isIdentifier(node) && node.text === "IntakeApplicationStage",
  ).map(() => path.relative(sourceRootPath, filename)));
  assert.deepEqual(
    forbiddenStageAliases,
    [],
    "the intake-prefixed application stage alias must stay absent",
  );
  assert.equal(
    moduleReferences(applicationContract.sourceFile).length,
    0,
    "the application core contract must stay a leaf",
  );
  const intakeContractImports = importsFrom(
    intakeContract.sourceFile,
    intakeOperationContractPath,
    applicationContractPath,
  );
  assert.equal(intakeContractImports.length, 1);
  assert.equal(intakeContractImports[0].importClause?.isTypeOnly, true);
  assert.equal(moduleReferences(intakeContract.sourceFile).length, 1);

  const apiContractImports = importsFrom(
    intakeApi.sourceFile,
    intakeOperationApiPath,
    intakeOperationContractPath,
  );
  assert.equal(apiContractImports.length, 1);
  assert.equal(apiContractImports[0].importClause?.isTypeOnly, true);
  const apiBuilderImports = importsFrom(
    intakeApi.sourceFile,
    intakeOperationApiPath,
    operationRequestsPath,
  );
  assert.equal(apiBuilderImports.length, 1);
  const builderBindings = apiBuilderImports[0].importClause?.namedBindings;
  assert.ok(builderBindings && ts.isNamedImports(builderBindings));
  assert.deepEqual(
    builderBindings.elements.map((element) => [
      element.propertyName?.text ?? element.name.text,
      element.name.text,
    ]).sort(),
    [
      ["decideOperation", "decideOperation"],
      ["getOperation", "getOperation"],
      ["listPendingOperations", "listPendingOperations"],
      ["postOperationForm", "postOperationForm"],
    ],
  );
  assert.deepEqual(
    moduleReferences(intakeApi.sourceFile)
      .map(({ specifier }) => resolveLocalModule(intakeOperationApiPath, specifier))
      .sort(),
    [
      moduleStem(intakeOperationContractPath),
      moduleStem(operationRequestsPath),
    ].sort(),
  );
  const rawFetchCalls = descendants(
    intakeApi.sourceFile,
    (node) => ts.isCallExpression(node)
      && ((ts.isIdentifier(node.expression) && node.expression.text === "fetch")
        || (ts.isPropertyAccessExpression(node.expression)
          && node.expression.name.text === "fetch")),
  );
  assert.equal(rawFetchCalls.length, 0, "Intake API must use the shared transport owner");

  const refreshContractImports = importsFrom(
    intakeRefresh.sourceFile,
    intakeOperationRefreshPath,
    intakeOperationContractPath,
  );
  assert.equal(refreshContractImports.length, 1);
  assert.equal(refreshContractImports[0].importClause?.isTypeOnly, true);
  assert.equal(moduleReferences(intakeRefresh.sourceFile).length, 1);

  assert.equal(importsFrom(intakePanel.sourceFile, intakeOperationsPanelPath, rootApiPath).length, 0);
  assert.equal(
    importsFrom(intakePanel.sourceFile, intakeOperationsPanelPath, applicationContractPath).length,
    1,
  );
  assert.equal(
    importsFrom(intakePanel.sourceFile, intakeOperationsPanelPath, intakeOperationContractPath).length,
    1,
  );
  assert.equal(
    importsFrom(intakePanel.sourceFile, intakeOperationsPanelPath, intakeOperationApiPath).length,
    1,
  );
  assert.equal(
    importsFrom(
      intakePanel.sourceFile,
      intakeOperationsPanelPath,
      intakeOperationRefreshPath,
    ).length,
    1,
  );
  for (const record of records.filter(({ filename }) => isWithin(filename, intakeFeaturePath))) {
    assert.equal(
      moduleReferences(record.sourceFile).some(
        ({ specifier }) => resolveLocalModule(record.filename, specifier) === moduleStem(rootApiPath),
      ),
      false,
      `${path.relative(sourceRootPath, record.filename)} must not depend on api.ts`,
    );
  }

  const intakeEndpointPrefix = "/api/timeline/intake-operations";
  const endpointOwners = records
    .filter(({ sourceFile }) => descendants(
      sourceFile,
      (node) => (ts.isStringLiteralLike(node) && node.text.includes(intakeEndpointPrefix))
        || (ts.isTemplateExpression(node) && (
          node.head.text.includes(intakeEndpointPrefix)
          || node.templateSpans.some((span) => span.literal.text.includes(intakeEndpointPrefix))
        )),
    ).length > 0)
    .map(({ filename }) => filename);
  assert.deepEqual(endpointOwners, [intakeOperationApiPath]);
  assertNoOwnerForwarding(
    records,
    [
      intakeOperationContractPath,
      intakeOperationApiPath,
      intakeOperationRefreshPath,
    ],
    "an Intake owner",
  );
});

test("200-row workbook UX keeps the preview bounded and explains both Agent and direct import", async () => {
  const records = await sourceRecords();
  const panel = recordFor(records, intakeOperationsPanelPath).sourceFile.text;
  const dialog = recordFor(records, timelineWorkbookDialogPath).sourceFile.text;
  const chat = recordFor(records, chatPagePath).sourceFile.text;

  assert.match(panel, /overflow-y-auto/);
  assert.match(panel, /scrollbar-gutter:stable/);
  assert.match(panel, /已选 \$\{includedCount\}/);
  assert.match(dialog, /每次最多 200 条/);
  assert.doesNotMatch(dialog, /超过 200 条的部分不会导入/);
  assert.match(dialog, /如果你不想使用表格模板，也可配置好大模型后通过求职助手进行智能化分析与导入/);
  assert.match(dialog, /w-full max-w-2xl/);
  assert.match(dialog, /download_job_import_template/);
  assert.match(dialog, /open_job_import_template/);
  assert.match(dialog, /拖入模板，或从电脑选择/);
  assert.match(dialog, /Drop the template here or choose one from your computer/);
  assert.match(dialog, /请使用 CareerDesk 表格模板/);
  assert.match(dialog, /Please use the CareerDesk workbook template/);
  assert.match(dialog, /单次最多200 条记录，若超过200条，可分批次上传/);
  assert.match(dialog, /Up to 200 records per upload\. If you have more than 200 records, upload them in batches\./);
  assert.doesNotMatch(dialog, /最大 10 MB/);
  assert.doesNotMatch(dialog, /10 MB maximum/);
  assert.match(dialog, /下载 CareerDesk 表格模板/);
  assert.match(dialog, /Download CareerDesk workbook template/);
  assert.match(dialog, /l\("表格模板", "Workbook template"\)/);
  assert.doesNotMatch(dialog, /示例表格/);
  assert.doesNotMatch(dialog, /example workbook/);
  assert.match(dialog, /打开模板/);
  assert.match(dialog, /Open template/);
  assert.doesNotMatch(dialog, /打开表格/);
  assert.doesNotMatch(dialog, /Open workbook/);
  assert.match(dialog, /text-sm font-semibold/);
  assert.match(chat, /请帮我批量导入这份表格中的岗位，并生成可核对的导入预览/);
  assert.match(chat, /已识别 CareerDesk 标准表格，将由本地代码读取，不使用大模型；一次最多处理 200 条/);
  assert.match(chat, /非标准表格需要由当前模型理解，可能漏掉或识别不准；一次最多处理 200 条/);
  assert.match(chat, /\[CAREERDESK_STANDARD_ROWS_V1\]/);
  assert.match(chat, /if \(explicitlyCancelled\) \{[\s\S]*?setError\(null\)/);
  assert.doesNotMatch(chat, /已停止接收，草稿和附件已恢复/);
});

test("completed intake notices disappear automatically without changing saved rows", async () => {
  const records = await sourceRecords();
  const panel = recordFor(records, intakeOperationsPanelPath).sourceFile.text;

  assert.match(panel, /window\.setTimeout\(\(\) => \{[\s\S]*setNotice[\s\S]*2000/);
  assert.doesNotMatch(panel, /关闭岗位导入结果提示|Dismiss role import result/);
});

test("Intake requests have bounded lifecycle cancellation and uncertainty retention", async () => {
  const records = await sourceRecords();
  const intakeApi = recordFor(records, intakeOperationApiPath);
  const intakePanel = recordFor(records, intakeOperationsPanelPath);

  assert.equal(callsNamed(intakeApi.sourceFile, "withRequestTimeout").length, 0);
  const builderCallCounts = new Map([
    ["listPendingOperations", ["INTAKE_LIST_TIMEOUT_MESSAGE"]],
    ["getOperation", ["INTAKE_STATUS_TIMEOUT_MESSAGE"]],
    ["decideOperation", ["INTAKE_COMMAND_TIMEOUT_MESSAGE", "INTAKE_COMMAND_TIMEOUT_MESSAGE"]],
  ]);
  for (const [builder, expectedMessages] of builderCallCounts) {
    const calls = callsNamed(intakeApi.sourceFile, builder);
    assert.equal(calls.length, expectedMessages.length, builder);
    const messages = calls.map((call) => {
      const options = call.arguments.at(-1);
      assert.ok(options && ts.isObjectLiteralExpression(options), `${builder} must pass options`);
      const messageProperty = options.properties.find((property) => (
        ts.isPropertyAssignment(property)
        && ts.isIdentifier(property.name)
        && property.name.text === "timeoutMessage"
      ));
      assert.ok(messageProperty, `${builder} must keep its intake-owned timeout message`);
      assert.ok(ts.isIdentifier(messageProperty.initializer));
      const initProperty = options.properties.find((property) => (
        ts.isShorthandPropertyAssignment(property) && property.name.text === "init"
      ));
      assert.ok(initProperty, `${builder} must forward the caller lifecycle signal`);
      return messageProperty.initializer.text;
    });
    assert.deepEqual(messages.sort(), expectedMessages.slice().sort(), builder);
  }

  const panelCalls = new Map([
    ["getPendingIntakeOperations", ["{signal:lifecycleAbortRef.current?.signal}"]],
    [
      "approveIntakeOperation",
      ["operation.operation_id", "requestedExcludedRows", "{signal:requestSignal}"],
    ],
    ["rejectIntakeOperation", ["operation.operation_id", "{signal:requestSignal}"]],
  ]);
  for (const [name, expectedArguments] of panelCalls) {
    const calls = callsNamed(intakePanel.sourceFile, name);
    assert.equal(calls.length, 1, `${name} must have one Panel call site`);
    assert.deepEqual(
      calls[0].arguments.map((argument) => compactNodeText(argument, intakePanel.sourceFile)),
      expectedArguments,
    );
  }
  const exactReadArguments = callsNamed(intakePanel.sourceFile, "getIntakeOperation")
    .map((call) => call.arguments.map((argument) => (
      compactNodeText(argument, intakePanel.sourceFile)
    )));
  assert.deepEqual(exactReadArguments, [
    ["operationId", "{signal:lifecycleAbortRef.current?.signal}"],
    ["operation.operation_id", "{signal:requestSignal}"],
  ]);

  const abortControllers = descendants(
    intakePanel.sourceFile,
    (node) => ts.isNewExpression(node)
      && ts.isIdentifier(node.expression)
      && node.expression.text === "AbortController",
  );
  assert.equal(abortControllers.length, 1, "the Panel must own one lifecycle controller");
  const abortCalls = descendants(
    intakePanel.sourceFile,
    (node) => ts.isCallExpression(node)
      && ts.isPropertyAccessExpression(node.expression)
      && node.expression.name.text === "abort",
  );
  assert.equal(abortCalls.length, 1, "the lifecycle controller must abort on cleanup");

  for (const helper of INTAKE_REFRESH_EXPORTS) {
    assert.equal(callsNamed(intakePanel.sourceFile, helper).length, 1);
  }
  const panelText = compactNodeText(intakePanel.sourceFile, intakePanel.sourceFile);
  assert.match(
    panelText,
    /constprotectedOperationIds=newSet\(protectedOperationIdsRef\.current\)/,
    "each accepted list response must freeze its protected-ID snapshot before React updaters run",
  );

  const runActionDeclarations = descendants(
    intakePanel.sourceFile,
    (node) => ts.isFunctionDeclaration(node)
      && node.name?.text === "runAction",
  );
  assert.equal(runActionDeclarations.length, 1);
  const runAction = runActionDeclarations[0];
  const runActionText = compactNodeText(runAction, intakePanel.sourceFile);
  const protectIndex = runActionText.indexOf(
    "protectedOperationIdsRef.current.add(operation.operation_id)",
  );
  const firstAwaitIndex = runActionText.indexOf("await");
  assert.ok(protectIndex >= 0 && protectIndex < firstAwaitIndex);

  const runActionTryStatements = descendants(runAction, (node) => ts.isTryStatement(node));
  const actionTry = runActionTryStatements.find((statement) => statement.finallyBlock);
  assert.ok(actionTry?.finallyBlock, "runAction must release its action in finally");
  const finallyBlock = actionTry.finallyBlock;
  assert.equal(
    descendants(finallyBlock, (node) => ts.isAwaitExpression(node)).length,
    0,
    "background reconciliation must not extend the action busy state",
  );
  const finallyText = compactNodeText(finallyBlock, intakePanel.sourceFile);
  const releaseRefIndex = finallyText.indexOf("actionRef.current=null");
  const releaseStateIndex = finallyText.indexOf("setAction(");
  const refreshIndex = finallyText.indexOf("voidrefreshPendingOperations(false)");
  assert.ok(
    releaseRefIndex >= 0
      && releaseStateIndex > releaseRefIndex
      && refreshIndex > releaseStateIndex,
    "the action lock and UI state must release before background reconciliation",
  );

  const toggleDeclarations = descendants(
    intakePanel.sourceFile,
    (node) => ts.isFunctionDeclaration(node)
      && node.name?.text === "togglePosition",
  );
  assert.equal(toggleDeclarations.length, 1);
  const toggleText = compactNodeText(toggleDeclarations[0], intakePanel.sourceFile);
  const retainUnknownIndex = toggleText.indexOf(
    "if(protectedOperationIdsRef.current.has(operationId))returncurrent",
  );
  const clearErrorIndex = toggleText.indexOf("deletenext[operationId]");
  assert.ok(
    retainUnknownIndex >= 0 && clearErrorIndex > retainUnknownIndex,
    "editing rows must not hide an operation whose canonical state is still unknown",
  );

  const canonicalDeclarations = descendants(
    intakePanel.sourceFile,
    (node) => ts.isFunctionDeclaration(node)
      && node.name?.text === "applyCanonicalOperation",
  );
  assert.equal(canonicalDeclarations.length, 1);
  assert.match(
    compactNodeText(canonicalDeclarations[0], intakePanel.sourceFile),
    /protectedOperationIdsRef\.current\.delete\(operation\.operation_id\)/,
  );
});
