import { withRequestTimeout } from "../../shared/api/requestTimeout.ts";
import { getJson, postForm, postJson } from "../../shared/api/transport.ts";
import type {
  AttachmentUploadResponse,
  ChatRecoveryScope,
  ChatTurnStatus,
} from "./chatContract";

const CHAT_TIMEOUT_MESSAGE = {
  zhCN: "响应超时，系统会继续核对这次操作的最终结果。",
  en: "The response timed out. CareerDesk will keep checking the final outcome.",
} as const;

export function getChatTurnStatus(
  clientTurnId: string,
  init?: Pick<RequestInit, "signal">,
): Promise<ChatTurnStatus> {
  return withRequestTimeout((signal) =>
    getJson<ChatTurnStatus>(
      `/api/chat/turns/${encodeURIComponent(clientTurnId)}/status`,
      { cache: "no-store", signal },
    ), init?.signal, CHAT_TIMEOUT_MESSAGE);
}

export function getChatRecoveryScope(
  init?: Pick<RequestInit, "signal">,
): Promise<ChatRecoveryScope> {
  return withRequestTimeout((signal) =>
    getJson<ChatRecoveryScope>(
      "/api/chat/recovery-scope",
      { cache: "no-store", signal },
    ), init?.signal, CHAT_TIMEOUT_MESSAGE);
}

export function cancelChatTurnIfAbsent(
  clientTurnId: string,
  init?: Pick<RequestInit, "signal">,
): Promise<ChatTurnStatus> {
  return withRequestTimeout((signal) =>
    postJson<ChatTurnStatus>(
      `/api/chat/turns/${encodeURIComponent(clientTurnId)}/cancel-if-absent`,
      {},
      { signal },
    ), init?.signal, CHAT_TIMEOUT_MESSAGE);
}

export function cancelChatTurn(
  clientTurnId: string,
  init?: Pick<RequestInit, "signal">,
): Promise<ChatTurnStatus> {
  return withRequestTimeout((signal) =>
    postJson<ChatTurnStatus>(
      `/api/chat/turns/${encodeURIComponent(clientTurnId)}/cancel`,
      {},
      { signal },
    ), init?.signal, CHAT_TIMEOUT_MESSAGE);
}

export function uploadChatAttachment(
  form: FormData,
  init?: Pick<RequestInit, "signal">,
): Promise<AttachmentUploadResponse> {
  return postForm<AttachmentUploadResponse>("/api/uploads", form, init);
}
