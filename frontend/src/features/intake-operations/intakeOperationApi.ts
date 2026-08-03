import {
  decideOperation,
  getOperation,
  listPendingOperations,
  postOperationForm,
} from "../../shared/api/operationRequests.ts";
import type { IntakeOperation } from "./intakeOperationContract";

const BASE_PATH = "/api/timeline/intake-operations";
const INTAKE_LIST_TIMEOUT_MESSAGE = {
  zhCN: "待确认岗位列表读取超时，请重试。",
  en: "Loading pending role imports timed out. Try again.",
} as const;
const INTAKE_STATUS_TIMEOUT_MESSAGE = {
  zhCN: "岗位变更状态读取超时，请继续核对。",
  en: "Checking the role import timed out. Keep checking its final state.",
} as const;
const INTAKE_COMMAND_TIMEOUT_MESSAGE = {
  zhCN: "岗位变更操作响应超时；结果仍待核对。",
  en: "The role import action timed out; its final state still needs verification.",
} as const;
const INTAKE_UPLOAD_TIMEOUT_MESSAGE = {
  zhCN: "表格读取超时，请确认文件大小后重试。",
  en: "Reading the workbook timed out. Check its size and try again.",
} as const;

type WorkbookIntakeResponse = {
  status: "preview" | "unrecognized" | "empty" | "superseded";
  operation_id: string | null;
  source_rows: number;
  skipped_rows: number;
  positions: IntakeOperation["positions"];
};

export function uploadWorkbookIntake(
  file: File,
  init?: Pick<RequestInit, "signal">,
): Promise<WorkbookIntakeResponse> {
  const form = new FormData();
  form.append("file", file);
  return postOperationForm<WorkbookIntakeResponse>(`${BASE_PATH}/file`, form, {
    init,
    timeoutMessage: INTAKE_UPLOAD_TIMEOUT_MESSAGE,
  });
}

// Model output can propose an import, but only these trusted same-origin APIs may commit it.
export function getPendingIntakeOperations(
  init?: Pick<RequestInit, "signal">,
): Promise<IntakeOperation[]> {
  return listPendingOperations(BASE_PATH, {
    init,
    timeoutMessage: INTAKE_LIST_TIMEOUT_MESSAGE,
  });
}

export function getIntakeOperation(
  operationId: string,
  init?: Pick<RequestInit, "signal">,
): Promise<IntakeOperation> {
  return getOperation(BASE_PATH, operationId, {
    init,
    timeoutMessage: INTAKE_STATUS_TIMEOUT_MESSAGE,
  });
}

export function approveIntakeOperation(
  operationId: string,
  excludeIndexes: number[],
  init?: Pick<RequestInit, "signal">,
): Promise<IntakeOperation> {
  return decideOperation(BASE_PATH, operationId, "approve", {
    init,
    timeoutMessage: INTAKE_COMMAND_TIMEOUT_MESSAGE,
    payload: { exclude_indexes: excludeIndexes },
  });
}

export function rejectIntakeOperation(
  operationId: string,
  init?: Pick<RequestInit, "signal">,
): Promise<IntakeOperation> {
  return decideOperation(BASE_PATH, operationId, "reject", {
    init,
    timeoutMessage: INTAKE_COMMAND_TIMEOUT_MESSAGE,
  });
}
