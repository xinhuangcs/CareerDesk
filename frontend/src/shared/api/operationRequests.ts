import { withRequestTimeout } from "./requestTimeout.ts";
import type { LocalizedRuntimeMessage } from "./runtimeLocale.ts";
import { getJson, postForm, postJson } from "./transport.ts";

export const OPERATION_TIMEOUT_MESSAGE = {
  zhCN: "响应超时，系统会继续核对这次操作的最终结果。",
  en: "The response timed out. CareerDesk will keep checking the final outcome.",
} as const satisfies LocalizedRuntimeMessage;

export type OperationRequestOptions = {
  init?: Pick<RequestInit, "signal">;
  timeoutMessage?: string | LocalizedRuntimeMessage;
};

function messageFor(options?: OperationRequestOptions): string | LocalizedRuntimeMessage {
  return options?.timeoutMessage ?? OPERATION_TIMEOUT_MESSAGE;
}

export async function listPendingOperations<T>(
  basePath: string,
  options?: OperationRequestOptions,
): Promise<T[]> {
  const response = await withRequestTimeout(
    (signal) => getJson<{ operations: T[] }>(
      `${basePath}/pending`,
      { cache: "no-store", signal },
    ),
    options?.init?.signal,
    messageFor(options),
  );
  return response.operations;
}

export async function listOperationsByClientTurn<T>(
  basePath: string,
  clientTurnId: string,
  options?: OperationRequestOptions,
): Promise<T[]> {
  const response = await withRequestTimeout(
    (signal) => getJson<{ operations: T[] }>(
      `${basePath}/by-client-turn/${encodeURIComponent(clientTurnId)}`,
      { cache: "no-store", signal },
    ),
    options?.init?.signal,
    messageFor(options),
  );
  return response.operations;
}

export async function listOperationsAt<T>(
  path: string,
  options?: OperationRequestOptions,
): Promise<T[]> {
  const response = await withRequestTimeout(
    (signal) => getJson<{ operations: T[] }>(path, { cache: "no-store", signal }),
    options?.init?.signal,
    messageFor(options),
  );
  return response.operations;
}

export function getOperation<T>(
  basePath: string,
  operationId: string,
  options?: OperationRequestOptions,
): Promise<T> {
  return withRequestTimeout(
    (signal) => getJson<T>(
      `${basePath}/${encodeURIComponent(operationId)}`,
      { cache: "no-store", signal },
    ),
    options?.init?.signal,
    messageFor(options),
  );
}

export function getOperationCommandStatus<T>(
  commandBasePath: string,
  commandId: string,
  options?: OperationRequestOptions,
): Promise<T> {
  return withRequestTimeout(
    (signal) => getJson<T>(
      `${commandBasePath}/${encodeURIComponent(commandId)}`,
      { cache: "no-store", signal },
    ),
    options?.init?.signal,
    messageFor(options),
  );
}

export function decideOperation<T>(
  basePath: string,
  operationId: string,
  decision: "approve" | "reject",
  options?: OperationRequestOptions & { payload?: object },
): Promise<T> {
  return withRequestTimeout(
    (signal) => postJson<T>(
      `${basePath}/${encodeURIComponent(operationId)}/${decision}`,
      options?.payload ?? {},
      { signal },
    ),
    options?.init?.signal,
    messageFor(options),
  );
}

export function postOperationForm<T>(
  path: string,
  form: FormData,
  options?: OperationRequestOptions,
): Promise<T> {
  return withRequestTimeout(
    (signal) => postForm<T>(path, form, { signal }),
    options?.init?.signal,
    messageFor(options),
  );
}

export function postOperationUndoCommand<T>(
  basePath: string,
  operationId: string,
  commandId: string,
  options?: OperationRequestOptions,
): Promise<T> {
  return withRequestTimeout(
    (signal) => postJson<T>(
      `${basePath}/${encodeURIComponent(operationId)}/undo`,
      { command_id: commandId },
      { signal },
    ),
    options?.init?.signal,
    messageFor(options),
  );
}

export function postOperationCommand<T>(
  path: string,
  payload: object,
  options?: OperationRequestOptions,
): Promise<T> {
  return withRequestTimeout(
    (signal) => postJson<T>(path, payload, { signal }),
    options?.init?.signal,
    messageFor(options),
  );
}

export async function postOperationList<T>(
  path: string,
  payload: object,
  options?: OperationRequestOptions,
): Promise<T[]> {
  const response = await withRequestTimeout(
    (signal) => postJson<{ operations: T[] }>(path, payload, { signal }),
    options?.init?.signal,
    messageFor(options),
  );
  return response.operations;
}
