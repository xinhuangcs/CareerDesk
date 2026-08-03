import assert from "node:assert/strict";
import path from "node:path";
import { test } from "node:test";

import ts from "typescript";

import {
  assertNoOwnerForwarding,
  descendants,
  exportedNames,
  importsFrom,
  moduleReferences,
  recordFor,
  runtimeDefinitionNames,
  sourceRecords,
  sourceRootPath,
  typeDefinitionNames,
} from "../test-support/typescriptArchitecture.mjs";

const rootApiPath = path.join(sourceRootPath, "api.ts");
const transportPath = path.join(sourceRootPath, "shared", "api", "transport.ts");
const requestTimeoutPath = path.join(sourceRootPath, "shared", "api", "requestTimeout.ts");
const operationRequestsPath = path.join(
  sourceRootPath,
  "shared",
  "api",
  "operationRequests.ts",
);
const timelinePath = path.join(sourceRootPath, "features", "timeline");
const timelineContractPath = path.join(timelinePath, "timelineContract.ts");
const timelinePagePath = path.join(timelinePath, "TimelinePage.tsx");
const resumeContractPath = path.join(
  sourceRootPath,
  "features",
  "resumes",
  "resumeContract.ts",
);
const questionsPagePath = path.join(
  sourceRootPath,
  "features",
  "questions",
  "QuestionsPage.tsx",
);
const libraryPagePath = path.join(
  sourceRootPath,
  "features",
  "library",
  "LibraryPage.tsx",
);
const grillPath = path.join(sourceRootPath, "features", "grill");
const grillContractPath = path.join(grillPath, "grillContract.ts");
const grillApiPath = path.join(grillPath, "grillApi.ts");
const grillPagePath = path.join(grillPath, "GrillPage.tsx");
const grillLabPagePath = path.join(grillPath, "GrillLabPage.tsx");
const chatPath = path.join(sourceRootPath, "features", "chat");
const chatContractPath = path.join(chatPath, "chatContract.ts");
const chatApiPath = path.join(chatPath, "chatApi.ts");
const chatPagePath = path.join(chatPath, "ChatPage.tsx");
const operationsPanelPath = path.join(
  sourceRootPath,
  "features",
  "operations",
  "TrustedImmediateOperationsPanel.tsx",
);
const applicationCorePath = path.join(
  sourceRootPath,
  "features",
  "applications",
  "applicationContract.ts",
);
const applicationDeletePath = path.join(
  sourceRootPath,
  "features",
  "application-delete-operations",
);
const applicationDeleteContractPath = path.join(
  applicationDeletePath,
  "applicationDeleteOperationContract.ts",
);
const applicationDeleteApiPath = path.join(
  applicationDeletePath,
  "applicationDeleteOperationApi.ts",
);
const applicationDeletePanelPath = path.join(
  applicationDeletePath,
  "ApplicationDeleteOperationsPanel.tsx",
);
const applicationMergePath = path.join(
  sourceRootPath,
  "features",
  "application-merge-operations",
);
const applicationMergeContractPath = path.join(
  applicationMergePath,
  "applicationMergeOperationContract.ts",
);
const applicationMergeApiPath = path.join(
  applicationMergePath,
  "applicationMergeOperationApi.ts",
);
const applicationMergePanelPath = path.join(
  applicationMergePath,
  "ApplicationMergeOperationsPanel.tsx",
);
const applicationUpdatePath = path.join(
  sourceRootPath,
  "features",
  "application-update-operations",
);
const applicationUpdateContractPath = path.join(
  applicationUpdatePath,
  "applicationUpdateOperationContract.ts",
);
const applicationUpdateApiPath = path.join(
  applicationUpdatePath,
  "applicationUpdateOperationApi.ts",
);
const reviewTimelineEntryEditPath = path.join(
  sourceRootPath,
  "features",
  "review-timeline-entry-edit-operations",
);
const reviewTimelineEntryEditContractPath = path.join(
  reviewTimelineEntryEditPath,
  "reviewTimelineEntryEditOperationContract.ts",
);
const reviewTimelineEntryEditApiPath = path.join(
  reviewTimelineEntryEditPath,
  "reviewTimelineEntryEditOperationApi.ts",
);
const reviewRecordPath = path.join(
  sourceRootPath,
  "features",
  "review-record-operations",
);
const reviewRecordContractPath = path.join(
  reviewRecordPath,
  "reviewRecordOperationContract.ts",
);
const reviewRecordApiPath = path.join(
  reviewRecordPath,
  "reviewRecordOperationApi.ts",
);
const reviewUndoPath = path.join(sourceRootPath, "features", "review-operations");
const reviewUndoContractPath = path.join(reviewUndoPath, "reviewUndoOperationContract.ts");
const reviewUndoApiPath = path.join(reviewUndoPath, "reviewUndoOperationApi.ts");
const reviewUndoPanelPath = path.join(reviewUndoPath, "ReviewUndoOperationsPanel.tsx");

const GRILL_CONTRACT_EXPORTS = [
  "GrillFlowResponse",
  "GrillProgress",
  "GrillQuestion",
  "QuestionSetItem",
  "ReadinessApplication",
  "ReadinessRequirement",
  "ReadinessResponse",
  "ReadinessResume",
  "ReplayAnswer",
  "SessionListItem",
  "SessionReplay",
];
const GRILL_CONTRACT_RUNTIME_EXPORTS = [
  "parseDeleteSetResponse",
  "parseGrillFlowResponse",
  "parseMutationResponse",
  "parseReadinessResponse",
  "parseReplayResponse",
  "parseSessionsResponse",
  "parseStatusResponse",
];
const GRILL_PAGE_API_EXPORTS = [
  "answerGrill",
  "deleteGrillSession",
  "deleteQuestionSet",
  "finalizeGrillSession",
  "generateSet",
  "getGrillSessionSummary",
  "getGrillSessions",
  "getReadiness",
  "resumeGrill",
  "skipGrill",
  "startGrill",
  "suspendGrill",
];
const GRILL_API_EXPORTS = ["claimGrillExperimentIntro", ...GRILL_PAGE_API_EXPORTS];
const CHAT_CONTRACT_EXPORTS = [
  "Attachment",
  "AttachmentUploadResponse",
  "ChatProposalOperationReference",
  "ChatProposalSurface",
  "ChatRecoveryScope",
  "ChatTurnState",
  "ChatTurnStatus",
];
const CHAT_API_EXPORTS = [
  "cancelChatTurn",
  "cancelChatTurnIfAbsent",
  "getChatRecoveryScope",
  "getChatTurnStatus",
  "uploadChatAttachment",
];
const TIMELINE_CONTRACT_EXPORTS = [
  "ApplicationDetail",
  "ApplicationStage",
  "Board",
  "BoardItem",
  "NextAction",
  "TimelineEntry",
  "TimelineOutcome",
  "TimelineStatistics",
];
const RESUME_CONTRACT_EXPORTS = ["ResumeItem", "ResumeJob", "ResumeJobDismissResponse", "ResumeText"];
const READ_CONTRACT_TYPE_OWNERS = new Map([
  ...TIMELINE_CONTRACT_EXPORTS
    .filter((symbol) => symbol !== "ApplicationStage")
    .map((symbol) => [symbol, timelineContractPath]),
  ...RESUME_CONTRACT_EXPORTS.map((symbol) => [symbol, resumeContractPath]),
]);
const TRUSTED_OPERATION_TYPE_OWNERS = new Map([
  ...[
    "ApplicationNextAction",
    "ApplicationPriority",
    "ApplicationStage",
    "ApplicationTimelineEntry",
  ].map((symbol) => [symbol, applicationCorePath]),
  ...[
    "ApplicationDeleteTimelineEntry",
    "ApplicationDeleteOperation",
    "ApplicationDeleteOperationState",
    "ApplicationDeleteResult",
  ].map((symbol) => [symbol, applicationDeleteContractPath]),
  ...[
    "ApplicationMergeApplication",
    "ApplicationMergeCounts",
    "ApplicationMergeFieldResolution",
    "ApplicationMergeFinalDestination",
    "ApplicationMergeOperation",
    "ApplicationMergeOperationState",
    "ApplicationMergeResult",
    "ApplicationMergeResumeRef",
  ].map((symbol) => [symbol, applicationMergeContractPath]),
  ...[
    "ApplicationUpdateCommandResult",
    "ApplicationUpdateEffect",
    "ApplicationUpdateOperation",
    "ApplicationUpdateOperationState",
    "ApplicationUpdateProjection",
    "ApplicationUpdateUndoBlockReason",
    "ApplicationUpdateUndoCommandStatus",
  ].map((symbol) => [symbol, applicationUpdateContractPath]),
  ...[
    "ReviewTimelineEntryEditApplicationProjection",
    "ReviewTimelineEntryEditCommandResult",
    "ReviewTimelineEntryEditEffect",
    "ReviewTimelineEntryEditField",
    "ReviewTimelineEntryEditOperation",
    "ReviewTimelineEntryEditUndoBlockReason",
    "ReviewTimelineEntryEditUndoCommandStatus",
    "ReviewTimelineEntryProjection",
  ].map((symbol) => [symbol, reviewTimelineEntryEditContractPath]),
  ...[
    "ReviewRecordDerivation",
    "ReviewRecordExtraction",
    "ReviewRecordMissingField",
    "ReviewRecordMode",
    "ReviewRecordOperation",
    "ReviewRecordOutcome",
    "ReviewRecordResult",
    "ReviewRecordState",
  ].map((symbol) => [symbol, reviewRecordContractPath]),
  ...[
    "ReviewUndoApplicationProjection",
    "ReviewUndoOperation",
    "ReviewUndoOperationState",
    "ReviewUndoTimelineEntry",
  ].map((symbol) => [symbol, reviewUndoContractPath]),
]);
const TRUSTED_OPERATION_API_OWNERS = new Map([
  ...[
    "approveApplicationDeleteOperation",
    "getApplicationDeleteOperation",
    "getPendingApplicationDeleteOperations",
    "rejectApplicationDeleteOperation",
  ].map((symbol) => [symbol, applicationDeleteApiPath]),
  ...[
    "approveApplicationMergeOperation",
    "getApplicationMergeOperation",
    "getPendingApplicationMergeOperations",
    "rejectApplicationMergeOperation",
  ].map((symbol) => [symbol, applicationMergeApiPath]),
  ...[
    "getApplicationUpdateOperation",
    "getApplicationUpdateOperationsByClientTurn",
    "getApplicationUpdateUndoCommandStatus",
    "undoApplicationUpdateOperation",
  ].map((symbol) => [symbol, applicationUpdateApiPath]),
  ...[
    "getReviewTimelineEntryEditOperation",
    "getReviewTimelineEntryEditOperationsByClientTurn",
    "getReviewTimelineEntryEditUndoCommandStatus",
    "undoReviewTimelineEntryEditOperation",
  ].map((symbol) => [symbol, reviewTimelineEntryEditApiPath]),
  ...[
    "getPendingReviewRecordClarifications",
    "getReviewRecordOperation",
    "getReviewRecordOperationsByClientTurn",
    "prepareReviewRecordUndoOperation",
  ].map((symbol) => [symbol, reviewRecordApiPath]),
  ...[
    "approveReviewUndoOperation",
    "getPendingReviewUndoOperations",
    "getReviewUndoOperation",
    "rejectReviewUndoOperation",
  ].map((symbol) => [symbol, reviewUndoApiPath]),
]);

function endpointOwners(records, endpoint, exact = false) {
  return records
    .filter(({ sourceFile }) => descendants(
      sourceFile,
      (node) => {
        if (ts.isStringLiteralLike(node)) {
          return exact ? node.text === endpoint : node.text.includes(endpoint);
        }
        return !exact && ts.isTemplateExpression(node) && (
          node.head.text.includes(endpoint)
          || node.templateSpans.some((span) => span.literal.text.includes(endpoint))
        );
      },
    ).length > 0)
    .map(({ filename }) => filename);
}

function namedImportBindings(sourceFile, importer, owner) {
  return importsFrom(sourceFile, importer, owner).flatMap((statement) => {
    const bindings = statement.importClause?.namedBindings;
    assert.ok(bindings && ts.isNamedImports(bindings));
    return bindings.elements.map((element) => element.propertyName?.text ?? element.name.text);
  });
}

test("Timeline and Resumes read contracts have one real feature owner", async () => {
  const records = await sourceRecords();
  const owners = [
    [recordFor(records, timelineContractPath), TIMELINE_CONTRACT_EXPORTS],
    [recordFor(records, resumeContractPath), RESUME_CONTRACT_EXPORTS],
  ];

  for (const [owner, expectedExports] of owners) {
    assert.deepEqual(
      exportedNames(owner.sourceFile).sort(),
      expectedExports.slice().sort(),
      path.relative(sourceRootPath, owner.filename),
    );
    const expectedModuleCount = owner.filename === timelineContractPath ? 2 : 0;
    assert.equal(
      moduleReferences(owner.sourceFile).length,
      expectedModuleCount,
      `${path.relative(sourceRootPath, owner.filename)} has unexpected dependencies`,
    );
    const defaultSurfaces = owner.sourceFile.statements.filter((statement) => (
      ts.isExportAssignment(statement)
      || (ts.canHaveModifiers(statement) && ts.getModifiers(statement)?.some(
        (modifier) => modifier.kind === ts.SyntaxKind.DefaultKeyword,
      ))
    ));
    assert.equal(defaultSurfaces.length, 0, `${owner.filename} must not export a default`);
  }

  for (const [symbol, expectedOwner] of READ_CONTRACT_TYPE_OWNERS) {
    const definitions = records
      .filter(({ sourceFile }) => typeDefinitionNames(sourceFile).includes(symbol))
      .map(({ filename }) => filename);
    const exportOwners = records
      .filter(({ sourceFile }) => exportedNames(sourceFile).includes(symbol))
      .map(({ filename }) => filename);
    assert.deepEqual(definitions, [expectedOwner], `${symbol} must have one type owner`);
    assert.deepEqual(exportOwners, [expectedOwner], `${symbol} must have one public owner`);
  }

  const callerImports = [
    [
      timelinePagePath,
      timelineContractPath,
      [
        "ApplicationDetail",
        "ApplicationStage",
        "Board",
        "BoardItem",
        "NextAction",
        "TimelineEntry",
        "TimelineOutcome",
        "TimelineStatistics",
      ],
    ],
    [libraryPagePath, timelineContractPath, ["Board", "BoardItem"]],
    [libraryPagePath, resumeContractPath, ["ResumeItem", "ResumeJob", "ResumeJobDismissResponse", "ResumeText"]],
  ];
  for (const [callerPath, ownerPath, expectedBindings] of callerImports) {
    const caller = recordFor(records, callerPath);
    assert.deepEqual(
      namedImportBindings(caller.sourceFile, callerPath, ownerPath).sort(),
      expectedBindings.slice().sort(),
      `${path.relative(sourceRootPath, callerPath)} must import the exact read contract`,
    );
  }

  assert.deepEqual(
    namedImportBindings(
      recordFor(records, timelineContractPath).sourceFile,
      timelineContractPath,
      applicationCorePath,
    ).sort(),
    ["ApplicationNextAction", "ApplicationPriority", "ApplicationStage", "ApplicationTimelineEntry"],
  );

  assertNoOwnerForwarding(
    records,
    [resumeContractPath],
    "a read contract owner",
  );
});

test("Grill and Chat contracts and APIs have one real feature owner", async () => {
  const records = await sourceRecords();
  const owners = [
    [recordFor(records, grillContractPath), [...GRILL_CONTRACT_EXPORTS, ...GRILL_CONTRACT_RUNTIME_EXPORTS]],
    [recordFor(records, grillApiPath), GRILL_API_EXPORTS],
    [recordFor(records, chatContractPath), CHAT_CONTRACT_EXPORTS],
    [recordFor(records, chatApiPath), CHAT_API_EXPORTS],
  ];

  for (const [owner, expectedExports] of owners) {
    assert.deepEqual(
      exportedNames(owner.sourceFile).sort(),
      expectedExports.slice().sort(),
      path.relative(sourceRootPath, owner.filename),
    );
    const defaultSurfaces = owner.sourceFile.statements.filter((statement) => (
      ts.isExportAssignment(statement)
      || (ts.canHaveModifiers(statement) && ts.getModifiers(statement)?.some(
        (modifier) => modifier.kind === ts.SyntaxKind.DefaultKeyword,
      ))
    ));
    assert.equal(defaultSurfaces.length, 0, `${owner.filename} must not export a default`);
  }

  const publicOwners = new Map([
    ...GRILL_CONTRACT_EXPORTS.map((symbol) => [symbol, grillContractPath]),
    ...GRILL_CONTRACT_RUNTIME_EXPORTS.map((symbol) => [symbol, grillContractPath]),
    ...GRILL_API_EXPORTS.map((symbol) => [symbol, grillApiPath]),
    ...CHAT_CONTRACT_EXPORTS.map((symbol) => [symbol, chatContractPath]),
    ...CHAT_API_EXPORTS.map((symbol) => [symbol, chatApiPath]),
  ]);
  for (const [symbol, expectedOwner] of publicOwners) {
    const exportOwners = records
      .filter(({ sourceFile }) => exportedNames(sourceFile).includes(symbol))
      .map(({ filename }) => filename);
    assert.deepEqual(exportOwners, [expectedOwner], `${symbol} must have one public owner`);
  }
  for (const [symbol, expectedOwner] of [
    ...GRILL_CONTRACT_EXPORTS.map((name) => [name, grillContractPath]),
    ...CHAT_CONTRACT_EXPORTS.map((name) => [name, chatContractPath]),
  ]) {
    const definitions = records
      .filter(({ sourceFile }) => typeDefinitionNames(sourceFile).includes(symbol))
      .map(({ filename }) => filename);
    assert.deepEqual(definitions, [expectedOwner], `${symbol} must have one type definition`);
  }
  for (const symbol of GRILL_CONTRACT_RUNTIME_EXPORTS) {
    const definitions = records
      .filter(({ sourceFile }) => runtimeDefinitionNames(sourceFile).includes(symbol))
      .map(({ filename }) => filename);
    assert.deepEqual(definitions, [grillContractPath], `${symbol} must have one runtime definition`);
  }
  for (const [symbol, expectedOwner] of [
    ...GRILL_API_EXPORTS.map((name) => [name, grillApiPath]),
    ...CHAT_API_EXPORTS.map((name) => [name, chatApiPath]),
  ]) {
    const definitions = records
      .filter(({ sourceFile }) => runtimeDefinitionNames(sourceFile).includes(symbol))
      .map(({ filename }) => filename);
    assert.deepEqual(definitions, [expectedOwner], `${symbol} must have one runtime owner`);
  }

  assert.equal(moduleReferences(recordFor(records, grillContractPath).sourceFile).length, 0);
  assert.equal(moduleReferences(recordFor(records, chatContractPath).sourceFile).length, 0);
  assertNoOwnerForwarding(
    records,
    [grillContractPath, grillApiPath, chatContractPath, chatApiPath],
    "a Grill or Chat feature owner",
  );
});

test("feature APIs own endpoints and callers directly consume them", async () => {
  const records = await sourceRecords();
  const grillApi = recordFor(records, grillApiPath);
  const grillPage = recordFor(records, grillPagePath);
  const grillLabPage = recordFor(records, grillLabPagePath);
  const chatApi = recordFor(records, chatApiPath);
  const chatPage = recordFor(records, chatPagePath);
  const operationsPanel = recordFor(records, operationsPanelPath);

  assert.deepEqual(endpointOwners(records, "/api/grill/"), [grillApiPath]);
  assert.deepEqual(endpointOwners(records, "/api/chat/turns/"), [chatApiPath]);
  assert.deepEqual(endpointOwners(records, "/api/chat/recovery-scope", true), [chatApiPath]);
  assert.deepEqual(endpointOwners(records, "/api/uploads", true), [chatApiPath]);

  assert.deepEqual(
    namedImportBindings(grillApi.sourceFile, grillApiPath, transportPath).sort(),
    ["del", "getJson", "postJson"],
  );
  assert.deepEqual(
    namedImportBindings(grillApi.sourceFile, grillApiPath, grillContractPath).sort(),
    [
      "GrillFlowResponse",
      "ReadinessResponse",
      "SessionListItem",
      "SessionReplay",
      ...GRILL_CONTRACT_RUNTIME_EXPORTS,
    ].sort(),
  );
  assert.deepEqual(
    namedImportBindings(chatApi.sourceFile, chatApiPath, transportPath).sort(),
    ["getJson", "postForm", "postJson"],
  );
  assert.deepEqual(
    namedImportBindings(chatApi.sourceFile, chatApiPath, requestTimeoutPath),
    ["withRequestTimeout"],
  );
  assert.deepEqual(
    namedImportBindings(chatApi.sourceFile, chatApiPath, chatContractPath).sort(),
    ["AttachmentUploadResponse", "ChatRecoveryScope", "ChatTurnStatus"],
  );

  for (const owner of [grillApi, chatApi]) {
    const rawFetchCalls = descendants(
      owner.sourceFile,
      (node) => ts.isCallExpression(node)
        && ts.isIdentifier(node.expression)
        && node.expression.text === "fetch",
    );
    assert.equal(rawFetchCalls.length, 0, `${owner.filename} must use shared transport`);
  }

  assert.deepEqual(
    namedImportBindings(grillPage.sourceFile, grillPagePath, grillApiPath).sort(),
    GRILL_PAGE_API_EXPORTS.sort(),
  );
  assert.deepEqual(
    namedImportBindings(grillLabPage.sourceFile, grillLabPagePath, grillApiPath),
    ["claimGrillExperimentIntro"],
  );
  assert.deepEqual(
    namedImportBindings(grillPage.sourceFile, grillPagePath, grillContractPath).sort(),
    [
      "GrillFlowResponse",
      "GrillQuestion",
      "QuestionSetItem",
      "ReadinessResponse",
      "SessionListItem",
      "SessionReplay",
    ].sort(),
  );
  assert.deepEqual(
    namedImportBindings(chatPage.sourceFile, chatPagePath, chatApiPath).sort(),
    ["cancelChatTurn", "getChatRecoveryScope", "getChatTurnStatus", "uploadChatAttachment"],
  );
  assert.deepEqual(
    namedImportBindings(chatPage.sourceFile, chatPagePath, chatContractPath).sort(),
    ["Attachment", "AttachmentUploadResponse"],
  );
  assert.deepEqual(
    namedImportBindings(operationsPanel.sourceFile, operationsPanelPath, chatApiPath).sort(),
    ["cancelChatTurnIfAbsent", "getChatTurnStatus"],
  );
  assert.deepEqual(
    namedImportBindings(operationsPanel.sourceFile, operationsPanelPath, chatContractPath),
    ["ChatTurnStatus"],
  );
});

test("trusted operation contracts and APIs no longer depend on the root domain module", async () => {
  const records = await sourceRecords();
  const applicationDeleteContract = recordFor(records, applicationDeleteContractPath);
  const applicationDeleteApi = recordFor(records, applicationDeleteApiPath);
  const applicationDeletePanel = recordFor(records, applicationDeletePanelPath);
  const applicationMergeContract = recordFor(records, applicationMergeContractPath);
  const applicationMergeApi = recordFor(records, applicationMergeApiPath);
  const applicationMergePanel = recordFor(records, applicationMergePanelPath);

  for (const [symbol, expectedOwner] of TRUSTED_OPERATION_TYPE_OWNERS) {
    const definitions = records
      .filter(({ sourceFile }) => typeDefinitionNames(sourceFile).includes(symbol))
      .map(({ filename }) => filename);
    const exportOwners = records
      .filter(({ sourceFile }) => exportedNames(sourceFile).includes(symbol))
      .map(({ filename }) => filename);
    assert.deepEqual(definitions, [expectedOwner], `${symbol} must have one type owner`);
    assert.deepEqual(
      exportOwners,
      symbol === "ApplicationStage" ? [expectedOwner, timelineContractPath] : [expectedOwner],
      `${symbol} must have the expected public owner`,
    );
  }
  for (const [symbol, expectedOwner] of TRUSTED_OPERATION_API_OWNERS) {
    const definitions = records
      .filter(({ sourceFile }) => runtimeDefinitionNames(sourceFile).includes(symbol))
      .map(({ filename }) => filename);
    const exportOwners = records
      .filter(({ sourceFile }) => exportedNames(sourceFile).includes(symbol))
      .map(({ filename }) => filename);
    assert.deepEqual(definitions, [expectedOwner], `${symbol} must have one runtime owner`);
    assert.deepEqual(exportOwners, [expectedOwner], `${symbol} must have one public owner`);
  }

  assert.equal(moduleReferences(recordFor(records, applicationCorePath).sourceFile).length, 0);
  for (const filename of [
    applicationDeleteContractPath,
    applicationDeleteApiPath,
    applicationDeletePanelPath,
    applicationMergeContractPath,
    applicationMergeApiPath,
    applicationMergePanelPath,
    applicationUpdateContractPath,
    applicationUpdateApiPath,
    reviewTimelineEntryEditContractPath,
    reviewTimelineEntryEditApiPath,
    reviewRecordContractPath,
    reviewRecordApiPath,
    reviewUndoContractPath,
    reviewUndoApiPath,
    operationsPanelPath,
    reviewUndoPanelPath,
  ]) {
    const record = recordFor(records, filename);
    assert.equal(
      importsFrom(record.sourceFile, filename, rootApiPath).length,
      0,
      `${path.relative(sourceRootPath, filename)} must not import api.ts`,
    );
  }

  for (const [contract, filename] of [
    [applicationDeleteContract, applicationDeleteContractPath],
    [applicationMergeContract, applicationMergeContractPath],
  ]) {
    assert.deepEqual(
      namedImportBindings(contract.sourceFile, filename, applicationCorePath).sort(),
      ["ApplicationNextAction", "ApplicationPriority", "ApplicationStage"],
    );
  }
  for (const [api, apiPath, contractPath, operationType, builderBindings] of [
    [
      applicationDeleteApi,
      applicationDeleteApiPath,
      applicationDeleteContractPath,
      "ApplicationDeleteOperation",
      ["decideOperation", "getOperation", "listPendingOperations", "postOperationCommand"],
    ],
    [
      applicationMergeApi,
      applicationMergeApiPath,
      applicationMergeContractPath,
      "ApplicationMergeOperation",
      ["decideOperation", "getOperation", "listPendingOperations"],
    ],
  ]) {
    assert.deepEqual(
      namedImportBindings(api.sourceFile, apiPath, operationRequestsPath).sort(),
      builderBindings,
    );
    assert.equal(importsFrom(api.sourceFile, apiPath, transportPath).length, 0);
    assert.equal(importsFrom(api.sourceFile, apiPath, requestTimeoutPath).length, 0);
    assert.deepEqual(
      namedImportBindings(api.sourceFile, apiPath, contractPath),
      [operationType],
    );
  }
  assert.deepEqual(
    namedImportBindings(
      applicationDeletePanel.sourceFile,
      applicationDeletePanelPath,
      applicationDeleteApiPath,
    ).sort(),
    [
      "approveApplicationDeleteOperation",
      "getApplicationDeleteOperation",
      "getPendingApplicationDeleteOperations",
      "rejectApplicationDeleteOperation",
    ],
  );
  assert.deepEqual(
    namedImportBindings(
      applicationDeletePanel.sourceFile,
      applicationDeletePanelPath,
      applicationDeleteContractPath,
    ),
    ["ApplicationDeleteOperation"],
  );
  assert.deepEqual(
    namedImportBindings(
      applicationMergePanel.sourceFile,
      applicationMergePanelPath,
      applicationMergeApiPath,
    ).sort(),
    [
      "approveApplicationMergeOperation",
      "getApplicationMergeOperation",
      "getPendingApplicationMergeOperations",
      "rejectApplicationMergeOperation",
    ],
  );
  assert.deepEqual(
    namedImportBindings(
      applicationMergePanel.sourceFile,
      applicationMergePanelPath,
      applicationMergeContractPath,
    ).sort(),
    [
      "ApplicationMergeCounts",
      "ApplicationMergeFieldResolution",
      "ApplicationMergeFinalDestination",
      "ApplicationMergeOperation",
      "ApplicationMergeResumeRef",
    ],
  );

  const endpointOwnerCases = [
    ["/api/timeline/application-delete-operations", applicationDeleteApiPath],
    ["/api/timeline/application-merge-operations", applicationMergeApiPath],
    ["/api/timeline/application-update-operations", applicationUpdateApiPath],
    ["/api/timeline/application-update-undo-commands", applicationUpdateApiPath],
    ["/api/reviews/timeline-entry-edit-operations", reviewTimelineEntryEditApiPath],
    ["/api/reviews/timeline-entry-edit-undo-commands", reviewTimelineEntryEditApiPath],
    ["/api/reviews/record-operations", reviewRecordApiPath],
    ["/api/reviews/timeline-applications", reviewUndoApiPath],
    ["/api/reviews/undo-operations", reviewUndoApiPath],
  ];
  for (const [endpoint, expectedOwner] of endpointOwnerCases) {
    assert.deepEqual(endpointOwners(records, endpoint), [expectedOwner], endpoint);
  }
  assertNoOwnerForwarding(
    records,
    [
      applicationDeleteContractPath,
      applicationDeleteApiPath,
      applicationMergeContractPath,
      applicationMergeApiPath,
      applicationUpdateContractPath,
      applicationUpdateApiPath,
      reviewTimelineEntryEditContractPath,
      reviewTimelineEntryEditApiPath,
      reviewRecordApiPath,
      reviewUndoContractPath,
      reviewUndoApiPath,
    ],
    "a trusted operation owner",
  );
});
