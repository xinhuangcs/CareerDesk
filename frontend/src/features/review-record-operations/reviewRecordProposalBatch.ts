import type {
  ReviewRecordExtraction,
  ReviewRecordOperation,
} from "./reviewRecordOperationContract";

export const MAX_REVIEW_RECORD_PROPOSAL_BATCH_ITEMS = 50;

export type ReviewRecordProposalBatchDecision = {
  operation_id: string;
  action: "approve" | "reject";
  edited_extraction?: ReviewRecordExtraction;
};

export type ReviewRecordProposalBatch = {
  clientTurnId: string;
  operations: ReviewRecordOperation[];
};

export type ReviewRecordProposalBatchCounts = {
  includedCount: number;
  publishCount: number;
  retainedDraftCount: number;
  excludedCount: number;
};

function isMissingJobIdentity(
  operation: ReviewRecordOperation,
  editedExtraction: ReviewRecordExtraction | undefined,
): boolean {
  const extraction = editedExtraction ?? operation.preview?.extraction;
  if (extraction !== undefined) {
    return !extraction.company?.trim() || !extraction.position?.trim();
  }
  return operation.preview?.missing.some(
    (item) => item.field === "company" || item.field === "position",
  ) ?? false;
}

export function countReviewRecordProposalBatch(
  operations: readonly ReviewRecordOperation[],
  includedOperationIds: ReadonlySet<string>,
  editedExtractions: ReadonlyMap<string, ReviewRecordExtraction> = new Map(),
): ReviewRecordProposalBatchCounts {
  let includedCount = 0;
  let retainedDraftCount = 0;
  for (const operation of operations) {
    if (!includedOperationIds.has(operation.operation_id)) continue;
    includedCount += 1;
    if (isMissingJobIdentity(
      operation,
      editedExtractions.get(operation.operation_id),
    )) {
      retainedDraftCount += 1;
    }
  }
  return {
    includedCount,
    publishCount: includedCount - retainedDraftCount,
    retainedDraftCount,
    excludedCount: operations.length - includedCount,
  };
}

export function groupReviewRecordProposalsByTurn(
  operations: readonly ReviewRecordOperation[],
): ReviewRecordProposalBatch[] {
  const batches = new Map<string, ReviewRecordOperation[]>();
  for (const operation of operations) {
    const batch = batches.get(operation.client_turn_id);
    if (batch === undefined) batches.set(operation.client_turn_id, [operation]);
    else batch.push(operation);
  }
  return [...batches].map(([clientTurnId, batchOperations]) => ({
    clientTurnId,
    operations: batchOperations,
  }));
}

export function buildReviewRecordProposalBatchDecisions(
  operations: readonly ReviewRecordOperation[],
  includedOperationIds: ReadonlySet<string>,
  rejectAll = false,
  editedExtractions: ReadonlyMap<string, ReviewRecordExtraction> = new Map(),
): ReviewRecordProposalBatchDecision[] {
  const clientTurnIds = new Set(operations.map((operation) => operation.client_turn_id));
  const operationIds = new Set(operations.map((operation) => operation.operation_id));
  if (operations.length === 0
      || operations.length > MAX_REVIEW_RECORD_PROPOSAL_BATCH_ITEMS
      || clientTurnIds.size !== 1
      || operationIds.size !== operations.length) {
    throw new Error("A review batch must contain 1–50 unique records from one turn");
  }
  for (const operationId of includedOperationIds) {
    if (!operationIds.has(operationId)) throw new Error("The review selection contains an unknown proposal");
  }
  for (const operationId of editedExtractions.keys()) {
    if (!operationIds.has(operationId)) throw new Error("The review edits contain an unknown proposal");
  }
  return operations.map((operation) => {
    const approved = !rejectAll && includedOperationIds.has(operation.operation_id);
    const editedExtraction = editedExtractions.get(operation.operation_id);
    return {
      operation_id: operation.operation_id,
      action: approved ? "approve" as const : "reject" as const,
      ...(approved && editedExtraction !== undefined
        ? { edited_extraction: editedExtraction }
        : {}),
    };
  });
}
