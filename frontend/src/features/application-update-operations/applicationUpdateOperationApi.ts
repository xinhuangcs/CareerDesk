import {
  getOperation,
  getOperationCommandStatus,
  listOperationsByClientTurn,
  postOperationUndoCommand,
} from "../../shared/api/operationRequests.ts";
import type {
  ApplicationUpdateOperation,
  ApplicationUpdateUndoCommandStatus,
} from "./applicationUpdateOperationContract";

const BASE_PATH = "/api/timeline/application-update-operations";
const UNDO_COMMAND_PATH = "/api/timeline/application-update-undo-commands";

export function getApplicationUpdateOperationsByClientTurn(
  clientTurnId: string,
  init?: Pick<RequestInit, "signal">,
): Promise<ApplicationUpdateOperation[]> {
  return listOperationsByClientTurn(BASE_PATH, clientTurnId, { init });
}

export function getApplicationUpdateOperation(
  operationId: string,
  init?: Pick<RequestInit, "signal">,
): Promise<ApplicationUpdateOperation> {
  return getOperation(BASE_PATH, operationId, { init });
}

export function undoApplicationUpdateOperation(
  operationId: string,
  commandId: string,
  init?: Pick<RequestInit, "signal">,
): Promise<ApplicationUpdateOperation> {
  return postOperationUndoCommand(BASE_PATH, operationId, commandId, { init });
}

export function getApplicationUpdateUndoCommandStatus(
  commandId: string,
  init?: Pick<RequestInit, "signal">,
): Promise<ApplicationUpdateUndoCommandStatus> {
  return getOperationCommandStatus(UNDO_COMMAND_PATH, commandId, { init });
}
