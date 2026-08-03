import {
  decideOperation,
  getOperation,
  listOperationsAt,
  listOperationsByClientTurn,
  postOperationCommand,
  postOperationList,
} from "../../shared/api/operationRequests.ts";
import type { ReviewUndoOperation } from "../review-operations/reviewUndoOperationContract";
import type { ReviewRecordOperation } from "./reviewRecordOperationContract";
import type {
  ReviewRecordProposalBatchDecision,
} from "./reviewRecordProposalBatch";

const BASE_PATH = "/api/reviews/record-operations";

export function getReviewRecordOperationsByClientTurn(
  clientTurnId: string,
  init?: Pick<RequestInit, "signal">,
): Promise<ReviewRecordOperation[]> {
  return listOperationsByClientTurn(BASE_PATH, clientTurnId, { init });
}

export function getReviewRecordOperation(
  operationId: string,
  init?: Pick<RequestInit, "signal">,
): Promise<ReviewRecordOperation> {
  return getOperation(BASE_PATH, operationId, { init });
}

export function getPendingReviewRecordClarifications(
  init?: Pick<RequestInit, "signal">,
): Promise<ReviewRecordOperation[]> {
  return listOperationsAt(`${BASE_PATH}/pending-clarifications`, { init });
}

export function getPendingReviewRecordConfirmations(
  init?: Pick<RequestInit, "signal">,
): Promise<ReviewRecordOperation[]> {
  return listOperationsAt(`${BASE_PATH}/pending-confirmations`, { init });
}

export function approveReviewRecordOperation(
  operationId: string,
  init?: Pick<RequestInit, "signal">,
): Promise<ReviewRecordOperation> {
  return decideOperation(BASE_PATH, operationId, "approve", { init });
}

export function rejectReviewRecordOperation(
  operationId: string,
  init?: Pick<RequestInit, "signal">,
): Promise<ReviewRecordOperation> {
  return decideOperation(BASE_PATH, operationId, "reject", { init });
}

export function decideReviewRecordOperationsByClientTurn(
  clientTurnId: string,
  decisions: ReviewRecordProposalBatchDecision[],
  init?: Pick<RequestInit, "signal">,
): Promise<ReviewRecordOperation[]> {
  return postOperationList(
    `${BASE_PATH}/by-client-turn/${encodeURIComponent(clientTurnId)}/decide`,
    { decisions },
    { init },
  );
}

export function prepareReviewRecordUndoOperation(
  operationId: string,
  init?: Pick<RequestInit, "signal">,
): Promise<ReviewUndoOperation> {
  return postOperationCommand(
    `${BASE_PATH}/${encodeURIComponent(operationId)}/prepare-undo`,
    {},
    { init },
  );
}
