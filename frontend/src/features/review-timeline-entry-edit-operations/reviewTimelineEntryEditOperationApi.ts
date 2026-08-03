import {
  getOperation,
  getOperationCommandStatus,
  listOperationsByClientTurn,
  postOperationUndoCommand,
} from "../../shared/api/operationRequests.ts";
import type {
  ReviewTimelineEntryEditOperation,
  ReviewTimelineEntryEditUndoCommandStatus,
} from "./reviewTimelineEntryEditOperationContract";

const BASE_PATH = "/api/reviews/timeline-entry-edit-operations";
const UNDO_COMMAND_PATH = "/api/reviews/timeline-entry-edit-undo-commands";

export function getReviewTimelineEntryEditOperationsByClientTurn(
  clientTurnId: string,
  init?: Pick<RequestInit, "signal">,
): Promise<ReviewTimelineEntryEditOperation[]> {
  return listOperationsByClientTurn(BASE_PATH, clientTurnId, { init });
}

export function getReviewTimelineEntryEditOperation(
  operationId: string,
  init?: Pick<RequestInit, "signal">,
): Promise<ReviewTimelineEntryEditOperation> {
  return getOperation(BASE_PATH, operationId, { init });
}

export function undoReviewTimelineEntryEditOperation(
  operationId: string,
  commandId: string,
  init?: Pick<RequestInit, "signal">,
): Promise<ReviewTimelineEntryEditOperation> {
  return postOperationUndoCommand(BASE_PATH, operationId, commandId, { init });
}

export function getReviewTimelineEntryEditUndoCommandStatus(
  commandId: string,
  init?: Pick<RequestInit, "signal">,
): Promise<ReviewTimelineEntryEditUndoCommandStatus> {
  return getOperationCommandStatus(UNDO_COMMAND_PATH, commandId, { init });
}
