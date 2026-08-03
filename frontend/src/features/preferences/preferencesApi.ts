import { withRequestTimeout } from "../../shared/api/requestTimeout";
import { getJson } from "../../shared/api/transport";
import type {
  PreferenceItemCommandPayload,
  PreferenceItemCommandSkeleton,
} from "./preferenceItemCommandContract";
import {
  preferenceItemCommandCancelRequest,
  preferenceItemCommandPutRequest,
  preferenceItemCommandReadRequest,
  preferencesReadRequest,
} from "./preferencesRequest";

export function getPreferencesSnapshot(
  init?: Pick<RequestInit, "signal">,
): Promise<unknown> {
  return withRequestTimeout((signal) => {
    const request = preferencesReadRequest(signal);
    return getJson<unknown>(request.url, request.init);
  }, init?.signal, {
    zhCN: "长期偏好读取超时，请重试。",
    en: "Loading long-term preferences timed out. Try again.",
  });
}

export function getPreferenceItemCommand(
  commandId: string,
  init?: Pick<RequestInit, "signal">,
): Promise<unknown> {
  return withRequestTimeout((signal) => {
    const request = preferenceItemCommandReadRequest(commandId, signal);
    return getJson<unknown>(request.url, request.init);
  }, init?.signal, {
    zhCN: "偏好修改状态读取超时，请继续核对。",
    en: "Checking the preference update timed out. Keep checking its final state.",
  });
}

export function putPreferenceItemCommand(
  commandId: string,
  payload: PreferenceItemCommandPayload,
  init?: Pick<RequestInit, "signal">,
): Promise<unknown> {
  return withRequestTimeout((signal) => {
    const request = preferenceItemCommandPutRequest(commandId, payload, signal);
    return getJson<unknown>(request.url, request.init);
  }, init?.signal, {
    zhCN: "偏好修改响应超时；结果仍待核对。",
    en: "The preference update timed out; its final state still needs verification.",
  });
}

export function cancelPreferenceItemCommandIfAbsent(
  commandId: string,
  skeleton: PreferenceItemCommandSkeleton,
  init?: Pick<RequestInit, "signal">,
): Promise<unknown> {
  return withRequestTimeout((signal) => {
    const request = preferenceItemCommandCancelRequest(commandId, skeleton, signal);
    return getJson<unknown>(request.url, request.init);
  }, init?.signal, {
    zhCN: "安全停止请求超时；结果仍待核对。",
    en: "The safe-stop request timed out; its final state still needs verification.",
  });
}
