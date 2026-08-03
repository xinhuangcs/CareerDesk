import { withRequestTimeout } from "../../shared/api/requestTimeout";
import { getJson } from "../../shared/api/transport";
import { preferenceOperationsByTurnReadRequest } from "./preferenceOperationRequest";

const PREFERENCE_OPERATION_TIMEOUT_MESSAGE = {
  zhCN: "响应超时，系统会继续核对这次操作的最终结果。",
  en: "The response timed out. CareerDesk will keep checking the final outcome.",
} as const;

export async function getPreferenceUpdateOperationsByClientTurn(
  clientTurnId: string,
  init?: Pick<RequestInit, "signal">,
): Promise<unknown> {
  const response = await withRequestTimeout<{ operations: unknown }>((signal) => {
    const request = preferenceOperationsByTurnReadRequest(clientTurnId, signal);
    return getJson<{ operations: unknown }>(request.url, request.init);
  }, init?.signal, PREFERENCE_OPERATION_TIMEOUT_MESSAGE);
  return response.operations;
}
