import {
  decideOperation,
  getOperation,
  listPendingOperations,
  postOperationCommand,
} from "../../shared/api/operationRequests.ts";
import type { ApplicationDeleteOperation } from "./applicationDeleteOperationContract";

const BASE_PATH = "/api/timeline/application-delete-operations";

export function getPendingApplicationDeleteOperations(): Promise<ApplicationDeleteOperation[]> {
  return listPendingOperations(BASE_PATH);
}

export function getApplicationDeleteOperation(
  operationId: string,
): Promise<ApplicationDeleteOperation> {
  return getOperation(BASE_PATH, operationId);
}

export function approveApplicationDeleteOperation(
  operationId: string,
): Promise<ApplicationDeleteOperation> {
  return decideOperation(BASE_PATH, operationId, "approve");
}

export function rejectApplicationDeleteOperation(
  operationId: string,
): Promise<ApplicationDeleteOperation> {
  return decideOperation(BASE_PATH, operationId, "reject");
}

export function prepareApplicationDeleteOperation(
  applicationId: number,
): Promise<ApplicationDeleteOperation> {
  return postOperationCommand(
    `/api/timeline/applications/${applicationId}/prepare-delete`,
    {},
  );
}
