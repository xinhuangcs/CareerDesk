import {
  decideOperation,
  getOperation,
  listPendingOperations,
} from "../../shared/api/operationRequests.ts";
import type { ApplicationMergeOperation } from "./applicationMergeOperationContract";

const BASE_PATH = "/api/timeline/application-merge-operations";

export function getPendingApplicationMergeOperations(): Promise<ApplicationMergeOperation[]> {
  return listPendingOperations(BASE_PATH);
}

export function getApplicationMergeOperation(
  operationId: string,
): Promise<ApplicationMergeOperation> {
  return getOperation(BASE_PATH, operationId);
}

export function approveApplicationMergeOperation(
  operationId: string,
): Promise<ApplicationMergeOperation> {
  return decideOperation(BASE_PATH, operationId, "approve");
}

export function rejectApplicationMergeOperation(
  operationId: string,
): Promise<ApplicationMergeOperation> {
  return decideOperation(BASE_PATH, operationId, "reject");
}
