import {
  decideOperation,
  getOperation,
  listPendingOperations,
  postOperationCommand,
} from "../../shared/api/operationRequests.ts";
import type { ReviewUndoOperation } from "./reviewUndoOperationContract";

const BASE_PATH = "/api/reviews/undo-operations";

export function getPendingReviewUndoOperations(): Promise<ReviewUndoOperation[]> {
  return listPendingOperations(BASE_PATH);
}

export function prepareTimelineReviewUndoOperation(
  applicationId: number,
  timelineEntryId: number,
  expectedFingerprint: string,
): Promise<ReviewUndoOperation> {
  return postOperationCommand(
    `/api/reviews/timeline-applications/${encodeURIComponent(applicationId)}`
    + `/timeline-entries/${encodeURIComponent(timelineEntryId)}/prepare-undo`,
    { expected_fingerprint: expectedFingerprint },
  );
}

export function getReviewUndoOperation(operationId: string): Promise<ReviewUndoOperation> {
  return getOperation(BASE_PATH, operationId);
}

export function approveReviewUndoOperation(operationId: string): Promise<ReviewUndoOperation> {
  return decideOperation(BASE_PATH, operationId, "approve");
}

export function rejectReviewUndoOperation(operationId: string): Promise<ReviewUndoOperation> {
  return decideOperation(BASE_PATH, operationId, "reject");
}
