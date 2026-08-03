import { Fragment, useCallback, useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import {
  cancelChatTurn,
  getChatRecoveryScope,
  getChatTurnStatus,
  uploadChatAttachment,
} from "./chatApi";
import {
  type Attachment,
  type AttachmentUploadResponse,
} from "./chatContract";
import {
  clearProposalRecovery,
  forgetProposalOperation,
  forgetReviewProposalTurn,
  readProposalRecovery,
  readSettledProposalOperations,
  readVisibleTrustedOperationTurns,
  proposalOperationFromServer,
  rememberProposalOperation,
  rememberReviewProposalTurn,
  rememberSettledProposalOperation,
  storeVisibleTrustedOperationTurns,
  type ProposalOperation,
} from "./chatProposalRecovery";
import { WRITE_HEADERS } from "../../shared/api/headers";
import { del, HttpError } from "../../shared/api/transport";
import {
  TrustedImmediateOperationsPanel,
  trustedImmediateOperationReceiptAnchorId,
} from "../operations/TrustedImmediateOperationsPanel";
import { PROPOSAL_PANELS, PROPOSAL_PANEL_SURFACES } from "./proposalPanelRegistry";
import {
  removeReviewSupplementComposerPrompt,
  reviewSupplementComposerText,
  reviewSupplementRequestFields,
} from "../review-record-operations/reviewSupplementContext";
import {
  getPendingReviewRecordConfirmations,
} from "../review-record-operations/reviewRecordOperationApi";
import {
  isReviewRecordOperation,
  reviewRecordIntegrityIssue,
} from "../review-record-operations/reviewRecordOperationContract";
import {
  markTrustedImmediateOperationTurnDispatched,
  recoverDispatchedTrustedImmediateOperationTurns,
  settleTrustedImmediateOperationDispatchedTurn,
  storeTrustedImmediateOperationTurns,
  trustedImmediateOperationTypeFromServer,
  type TrustedImmediateOperationType,
} from "../operations/trustedImmediateOperationOutbox";
import { ArrowUpIcon, FileIcon, ImageIcon, Logo, StopIcon } from "../../icons";
import { Markdown } from "../../Markdown";
import { chatUiActionsFromServer, type ChatUiAction } from "./chatUiActions";
import { currentOutputLocale, useLocale } from "../../i18n/localePreference";
import { useLocalizer } from "../../i18n/useLocalizer";
import {
  quickPromptRotation as getQuickPromptRotation,
  type PromptIcon,
} from "./chatQuickPrompts";
import { ChatAssistantProgress } from "./ChatAssistantProgress";
import { useChatTurnViewport } from "./useChatTurnViewport";

// It reads SSE through fetch because native EventSource cannot send the required POST body.

type ChatEvent =
  | { event: "message_delta"; data: { text: string } }
  | { event: "message_snapshot"; data: { text: string } }
  | {
      event: "tool_status";
      data: {
        tool: string;
        label: string;
        trusted_operation_type?: TrustedImmediateOperationType;
        proposal_surface?: unknown;
        proposal_operation_id?: unknown;
      };
    }
  | {
      event: "done";
      data: {
        session: string;
        message_id: string;
        client_turn_id: string;
        proposal_operations?: unknown;
        ui_actions?: unknown;
      };
    }
  | { event: "error"; data: ChatErrorData };

type ChatErrorData = {
  code: string;
  message: string;
  retryable: boolean;
  history_committed?: boolean;
  attachments?: "retained" | "consumed";
  client_turn_id: string;
  trace_id?: string;
};

type UiError = Partial<ChatErrorData> & { message: string };
type Message = {
  id: string;
  clientTurnId: string;
  role: "user" | "assistant";
  text: string;
  uiActions: ChatUiAction[];
};
type AbortCause = "clear" | "unmount" | null;
type InFlightTurn = { clientTurnId: string; attachments: Attachment[] };
const PROPOSAL_NOTICE_DURATION_MS = 2000;

function isStandardWorkbookAttachment(attachment: Attachment): boolean {
  return attachment.kind === "document"
    && /\.(?:xlsx|xls|csv|tsv)$/i.test(attachment.filename)
    && attachment.text.includes("[CAREERDESK_STANDARD_ROWS_V1]")
    && attachment.text.includes("[/CAREERDESK_STANDARD_ROWS_V1]");
}

function proposalOperationKey(operation: ProposalOperation): string {
  return `${operation.surface}:${operation.operationId}`;
}

function proposalOperationsFromServer(value: unknown): ProposalOperation[] | null {
  if (!Array.isArray(value) || value.length > 200) return null;
  const operations: ProposalOperation[] = [];
  const seen = new Set<string>();
  for (const item of value) {
    if (item === null || typeof item !== "object" || Array.isArray(item)) return null;
    const record = item as Record<string, unknown>;
    const operation = proposalOperationFromServer(record.surface, record.operation_id);
    if (operation === null) return null;
    const key = proposalOperationKey(operation);
    if (seen.has(key)) return null;
    seen.add(key);
    operations.push(operation);
  }
  return operations;
}

type ValidatedChatTurnStatus = {
  state: "absent" | "running" | "completed" | "unknown" | "cancelled";
  terminal: boolean;
  proposalOperations: ProposalOperation[];
};

function chatTurnStatusFromServer(
  value: unknown,
  expectedClientTurnId: string,
): ValidatedChatTurnStatus | null {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return null;
  const record = value as Record<string, unknown>;
  const states: ValidatedChatTurnStatus["state"][] = [
    "absent",
    "running",
    "completed",
    "unknown",
    "cancelled",
  ];
  if (record.client_turn_id !== expectedClientTurnId
      || typeof record.state !== "string"
      || !states.includes(record.state as ValidatedChatTurnStatus["state"])
      || typeof record.terminal !== "boolean") return null;
  const state = record.state as ValidatedChatTurnStatus["state"];
  if (record.terminal !== ["completed", "unknown", "cancelled"].includes(state)) return null;
  const proposalOperations = proposalOperationsFromServer(record.proposal_operations);
  return proposalOperations === null
    ? null
    : { state, terminal: record.terminal, proposalOperations };
}

// HashRouter links opened in a new tab require an explicit hash route.
const SETTINGS_NEW_TAB_HREF = `${import.meta.env.BASE_URL}#/settings`;

function makeId(): string {
  if (typeof globalThis.crypto?.randomUUID === "function") return globalThis.crypto.randomUUID();
  const bytes = new Uint8Array(16);
  if (typeof globalThis.crypto?.getRandomValues === "function") {
    globalThis.crypto.getRandomValues(bytes);
  } else {
    for (let index = 0; index < bytes.length; index += 1) {
      bytes[index] = Math.floor(Math.random() * 256);
    }
  }
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (value) => value.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function assertNever(value: never): never {
  throw new Error(`Unhandled trusted operation type: ${String(value)}`);
}

class ChatStreamError extends Error {
  readonly data: ChatErrorData;

  constructor(data: ChatErrorData) {
    super(data.message);
    this.name = "ChatStreamError";
    this.data = data;
  }
}

const CHAT_SERVER_ERROR_COPY: Readonly<Record<string, readonly [string, string]>> = {
  idempotency_key_reused: ["请求编号已被另一条内容使用，请重新发送。", "This request ID was already used for different content. Send again."],
  turn_in_progress: ["这一轮仍在处理中，请稍后继续核对。", "This turn is still processing. Check again shortly."],
  turn_cancelled: ["这一轮已安全取消，请重新发送。", "This turn was cancelled safely. Send it again."],
  session_busy: ["当前对话正在处理另一条请求，请稍后再试。", "This conversation is processing another request. Try again shortly."],
  turn_outcome_unknown: ["暂时无法确认这一轮的最终结果，请继续核对。", "The final outcome of this turn cannot be confirmed yet. Continue checking."],
  strict_offline: ["严格离线模式已阻止模型访问。", "Strict offline mode blocked model access."],
  model_not_configured: ["模型尚未配置，请前往设置完成配置。", "No model is configured. Complete setup in Settings."],
  model_capabilities_missing: ["模型能力信息不完整，请前往设置补齐。", "Model capability information is incomplete. Complete it in Settings."],
  image_unsupported: ["当前模型不支持图片输入，请移除图片或更换模型。", "The current model does not support image input. Remove the image or change models."],
  assistant_setup_failed: ["助手暂时无法启动，请检查模型配置后重试。", "The assistant could not start. Check model settings and retry."],
};

function localizedChatServerMessage(code: string): string {
  const pair = CHAT_SERVER_ERROR_COPY[code];
  const locale = currentOutputLocale();
  if (!pair) return locale === "en" ? "The request could not be completed. Try again." : "请求未能完成，请重试。";
  return locale === "en" ? pair[1] : pair[0];
}

function httpChatError(payload: unknown, status: number, clientTurnId: string): Error {
  const body = payload && typeof payload === "object" ? payload as Record<string, unknown> : null;
  if (body && typeof body.code === "string") {
    // Only an exact response identity with history=false proves the turn did not execute.
    // Cross-turn or corrupt responses remain ambiguous.
    const identityMatches = body.client_turn_id === clientTurnId;
    return new ChatStreamError({
      code: body.code,
      message: localizedChatServerMessage(body.code),
      retryable: body.retryable === true,
      history_committed: identityMatches && typeof body.history_committed === "boolean"
        ? body.history_committed
        : undefined,
      attachments: identityMatches
        && (body.attachments === "retained" || body.attachments === "consumed")
        ? body.attachments
        : undefined,
      client_turn_id: clientTurnId,
    });
  }
  const locale = currentOutputLocale();
  return new Error(locale === "en"
    ? `The request could not be completed (HTTP ${status}). Try again.`
    : `请求未能完成（HTTP ${status}），请重试。`);
}

const PROMPT_ICON_PATHS: Record<PromptIcon, string> = {
  replay: "M3.2 8a4.8 4.8 0 1 0 1.4-3.4M3.2 4.6v2.8h2.8",
  target: "M8 2.6a5.4 5.4 0 1 0 0 10.8 5.4 5.4 0 0 0 0-10.8ZM8 5.6a2.4 2.4 0 1 0 0 4.8 2.4 2.4 0 0 0 0-4.8ZM8 7.9v.2",
  clipboard: "M6 3.5H4.6A1 1 0 0 0 3.6 4.5v8a1 1 0 0 0 1 1h6.8a1 1 0 0 0 1-1v-8a1 1 0 0 0-1-1H10M6 3.5a1 1 0 0 1 1-1h2a1 1 0 0 1 1 1v.8H6zM6 8h4M6 10.4h4",
  bookmark: "M4.7 3.4h6.6v9.2L8 10.2 4.7 12.6z",
  board: "M3.3 3.6h3.2v8.8H3.3zM7.6 3.6h3.2v5.6H7.6zM11.9 3.6h3.2v8.8h-3.2z",
  pulse: "M2.2 8h2.7l1.5-3.6 2.6 7.2 1.5-3.6h3.3",
};

const QUICK_PROMPT_PAGE_SIZE = 4;

function PromptIconGlyph({ name }: { name: PromptIcon }) {
  return (
    <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor"
         strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <path d={PROMPT_ICON_PATHS[name]} />
    </svg>
  );
}

/** Parse complete SSE events and retain the final partial buffer. */
function parseSse(buffer: string): [ChatEvent[], string] {
  // Network chunks do not align to event boundaries, so retain the partial tail.
  const parts = buffer.split("\n\n");
  const rest = parts.pop() ?? "";
  const events: ChatEvent[] = [];
  for (const part of parts) {
    let event = "";
    const dataLines: string[] = [];
    for (const line of part.split("\n")) {
      if (line.startsWith(":")) continue; // Ignore keep-alive comment frames.
      if (line.startsWith("event:")) event = line.slice(6).trim();
      if (line.startsWith("data:")) dataLines.push(line.slice(5).trim());
    }
    if (event && dataLines.length > 0) {
      try {
        events.push({ event, data: JSON.parse(dataLines.join("\n")) } as ChatEvent);
      } catch {
        // Skip one malformed data frame while retaining other terminal frames in the batch.
      }
    }
  }
  return [events, rest];
}

export function ChatPage({ active = true }: { active?: boolean }) {
  const { locale } = useLocale();
  const l = useLocalizer();
  const quickPromptRotation = getQuickPromptRotation(locale);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [quickPromptPage, setQuickPromptPage] = useState(0);
  const [error, setError] = useState<UiError | null>(null);
  const [busy, setBusy] = useState(false);
  const [stopping, setStopping] = useState(false);
  const [attachments, setAttachments] = useState<Attachment[]>([]); // Attachments for the next message.
  const [uploading, setUploading] = useState(false);
  const [confirmClear, setConfirmClear] = useState(false); // Inline new-topic confirmation.
  const [clearingTopic, setClearingTopic] = useState(false);
  const [toolStatus, setToolStatus] = useState<string | null>(null); // Transient tool status cleared on output.
  const [operationRefreshSignal, setOperationRefreshSignal] = useState(0);
  const [trustedOperationRefreshSignal, setTrustedOperationRefreshSignal] = useState(0);
  const [trustedOperationConversationResetSignal, setTrustedOperationConversationResetSignal] = useState(0);
  const [proposalOperations, setProposalOperations] = useState<ProposalOperation[]>([]);
  const [proposalOperationTurnIds, setProposalOperationTurnIds] = useState<Record<string, string>>(
    {},
  );
  const proposalOperationsRef = useRef<ProposalOperation[]>([]);
  const proposalOperationTurnIdsRef = useRef<Record<string, string>>({});
  const proposalSettlementTimersRef = useRef(new Map<string, number>());
  const settledProposalOperationKeysRef = useRef<Set<string>>(new Set());
  const [trustedOperationRecoveryScope, setTrustedOperationRecoveryScope] = useState<string | null>(null);
  const [recoveryScopeError, setRecoveryScopeError] = useState("");
  const [recoveryScopeRetrySignal, setRecoveryScopeRetrySignal] = useState(0);
  const [reviewSupplementReference, setReviewSupplementReferenceState] = useState<string | null>(
    null,
  );
  // Persist only UUIDs issued by this tab, never message text, sessions, or model output.
  // sessionStorage supports refresh recovery and clears when the tab closes.
  const [trustedOperationTurnIds, setTrustedOperationTurnIds] = useState<string[]>([]);
  const [visibleTrustedOperationTurnIds, setVisibleTrustedOperationTurnIds] = useState<string[]>([]);
  const [uncertainTrustedOperationTurnIds, setUncertainTrustedOperationTurnIds] = useState<string[]>([]);
  const trustedOperationTurnIdsRef = useRef<Set<string>>(new Set());
  const sessionRef = useRef<string | null>(null); // Scoped to this tab; refresh starts a visible session.
  const draftTurnIdRef = useRef<string | null>(null); // Reuse unchanged failed drafts for completed replay.
  const busyRef = useRef(false);
  const stoppingRef = useRef(false);
  const stopRequestedTurnIdRef = useRef<string | null>(null);
  const cancelAcceptedTurnIdRef = useRef<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const abortCauseRef = useRef<AbortCause>(null);
  const uploadAbortRef = useRef<AbortController | null>(null);
  const attachmentsRef = useRef<Attachment[]>([]);
  const inFlightRef = useRef<InFlightTurn | null>(null);
  const releasedStoredRef = useRef(new Set<string>());
  const fileRef = useRef<HTMLInputElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);
  const newTopicButtonRef = useRef<HTMLButtonElement | null>(null);
  const newTopicCancelRef = useRef<HTMLButtonElement | null>(null);
  const newTopicControlRef = useRef<HTMLDivElement | null>(null);
  const reviewSupplementReferenceRef = useRef<string | null>(null);
  const hasMessages = messages.length > 0;
  const hasConversationContent = hasMessages
    || proposalOperations.length > 0
    || trustedOperationTurnIds.length > 0
    || uncertainTrustedOperationTurnIds.length > 0;
  const {
    composerRef,
    composerReservationRef,
    conversationLeadSpacerRef,
    latestUserMessageRef,
    messagesRef,
    positionLatestTurn,
    resetTurnViewport,
    turnSpacerRef,
  } = useChatTurnViewport({
    active,
    busy,
    composerDocked: hasConversationContent,
    messages,
    operationRefreshSignal,
    toolStatus,
  });

  const updateReviewSupplementReference = useCallback((reference: string | null) => {
    reviewSupplementReferenceRef.current = reference;
    setReviewSupplementReferenceState(reference);
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setTrustedOperationRecoveryScope(null);
    setRecoveryScopeError("");
    void getChatRecoveryScope({ signal: controller.signal })
      .then((response) => {
        if (!/^[0-9a-f]{64}$/.test(response.scope)) {
          throw new Error(l("无法验证当前会话的恢复信息", "Could not verify recovery information for this conversation"));
        }
        const recovered = recoverDispatchedTrustedImmediateOperationTurns(response.scope);
        const proposalRecovery = readProposalRecovery(response.scope);
        const settledProposalOperations = readSettledProposalOperations(response.scope);
        settledProposalOperationKeysRef.current = new Set(
          settledProposalOperations.map(proposalOperationKey),
        );
        const pendingProposalOperations = proposalRecovery.operations.filter(
          (operation) => !settledProposalOperationKeysRef.current.has(
            proposalOperationKey(operation),
          ),
        );
        const recoveredTurnIds = [
          ...new Set([...proposalRecovery.reviewTurnIds, ...recovered.turnIds]),
        ].slice(-128);
        const recoveredVisibleTurnIds = [
          ...new Set([
            ...readVisibleTrustedOperationTurns(response.scope),
            ...recovered.uncertainTurnIds,
          ]),
        ].filter((clientTurnId) => recoveredTurnIds.includes(clientTurnId));
        proposalOperationsRef.current = pendingProposalOperations;
        setProposalOperations(pendingProposalOperations);
        trustedOperationTurnIdsRef.current = new Set(recoveredTurnIds);
        setTrustedOperationTurnIds(recoveredTurnIds);
        setVisibleTrustedOperationTurnIds(recoveredVisibleTurnIds);
        setUncertainTrustedOperationTurnIds(recovered.uncertainTurnIds);
        setTrustedOperationRecoveryScope(response.scope);
      })
      .catch((reason: unknown) => {
        if (controller.signal.aborted) return;
        setTrustedOperationRecoveryScope(null);
        setRecoveryScopeError(
          reason instanceof Error ? reason.message : l("无法建立会话恢复通道", "Could not establish the conversation recovery channel"),
        );
      });
    return () => controller.abort();
  }, [recoveryScopeRetrySignal]);

  useEffect(() => {
    if (trustedOperationRecoveryScope === null) return;
    storeTrustedImmediateOperationTurns(
      trustedOperationRecoveryScope,
      trustedOperationTurnIds,
      uncertainTrustedOperationTurnIds,
    );
  }, [trustedOperationRecoveryScope, trustedOperationTurnIds,
    uncertainTrustedOperationTurnIds]);

  useEffect(() => {
    if (trustedOperationRecoveryScope === null) return;
    storeVisibleTrustedOperationTurns(
      trustedOperationRecoveryScope,
      visibleTrustedOperationTurnIds,
    );
  }, [trustedOperationRecoveryScope, visibleTrustedOperationTurnIds]);

  const resolveTrustedOperationTurn = useCallback((clientTurnId: string) => {
    setUncertainTrustedOperationTurnIds((current) => (
      current.includes(clientTurnId)
        ? current.filter((item) => item !== clientTurnId)
        : current
    ));
  }, []);

  const discardEmptyTrustedOperationTurn = useCallback((clientTurnId: string) => {
    if (trustedOperationRecoveryScope !== null) {
      forgetReviewProposalTurn(trustedOperationRecoveryScope, clientTurnId);
    }
    trustedOperationTurnIdsRef.current.delete(clientTurnId);
    setTrustedOperationTurnIds((current) => {
      if (!current.includes(clientTurnId)) return current;
      const next = current.filter((item) => item !== clientTurnId);
      return next;
    });
    setUncertainTrustedOperationTurnIds((current) => (
      current.includes(clientTurnId)
        ? current.filter((item) => item !== clientTurnId)
        : current
    ));
    setVisibleTrustedOperationTurnIds((current) => (
      current.includes(clientTurnId)
        ? current.filter((item) => item !== clientTurnId)
        : current
    ));
  }, [trustedOperationRecoveryScope]);

  const revealProposalOperation = useCallback((
    operation: ProposalOperation,
    clientTurnId?: string,
  ) => {
    const key = proposalOperationKey(operation);
    if (clientTurnId !== undefined) {
      proposalOperationTurnIdsRef.current = {
        ...proposalOperationTurnIdsRef.current,
        [key]: clientTurnId,
      };
      setProposalOperationTurnIds((current) => current[key] === clientTurnId
        ? current
        : { ...current, [key]: clientTurnId });
    }
    if (settledProposalOperationKeysRef.current.has(key)) return;
    if (proposalOperationsRef.current.some((item) => proposalOperationKey(item) === key)) {
      return;
    }
    const next = [...proposalOperationsRef.current, operation].slice(-200);
    proposalOperationsRef.current = next;
    setProposalOperations(next);
    if (trustedOperationRecoveryScope !== null) {
      rememberProposalOperation(trustedOperationRecoveryScope, operation);
    }
  }, [trustedOperationRecoveryScope]);

  const settleProposalOperation = useCallback((operation: ProposalOperation) => {
    const key = proposalOperationKey(operation);
    settledProposalOperationKeysRef.current.add(key);
    if (trustedOperationRecoveryScope !== null) {
      rememberSettledProposalOperation(trustedOperationRecoveryScope, operation);
      forgetProposalOperation(trustedOperationRecoveryScope, operation);
    }
    const previousTimer = proposalSettlementTimersRef.current.get(key);
    if (previousTimer !== undefined) window.clearTimeout(previousTimer);
    const timer = window.setTimeout(() => {
      proposalSettlementTimersRef.current.delete(key);
      proposalOperationsRef.current = proposalOperationsRef.current.filter(
        (item) => proposalOperationKey(item) !== key,
      );
      setProposalOperations(proposalOperationsRef.current);
      const nextTurnIds = { ...proposalOperationTurnIdsRef.current };
      delete nextTurnIds[key];
      proposalOperationTurnIdsRef.current = nextTurnIds;
      setProposalOperationTurnIds(nextTurnIds);
    }, PROPOSAL_NOTICE_DURATION_MS);
    proposalSettlementTimersRef.current.set(key, timer);
  }, [trustedOperationRecoveryScope]);

  const revealDiscoveredProposalOperations = useCallback((discoveries: {
    operation: ProposalOperation;
    clientTurnId: string;
  }[]) => {
    for (const discovery of discoveries) {
      revealProposalOperation(discovery.operation, discovery.clientTurnId);
    }
  }, [revealProposalOperation]);

  const discardCancelledTrustedOperationTurn = useCallback((
    clientTurnId: string,
    showRecoveryNotice = true,
  ) => {
    const discarded = proposalOperationsRef.current.filter(
      (operation) => proposalOperationTurnIdsRef.current[proposalOperationKey(operation)]
        === clientTurnId,
    );
    const discardedKeys = new Set(discarded.map(proposalOperationKey));
    if (discardedKeys.size > 0) {
      for (const key of discardedKeys) {
        const timer = proposalSettlementTimersRef.current.get(key);
        if (timer !== undefined) window.clearTimeout(timer);
        proposalSettlementTimersRef.current.delete(key);
      }
      proposalOperationsRef.current = proposalOperationsRef.current.filter(
        (operation) => !discardedKeys.has(proposalOperationKey(operation)),
      );
      setProposalOperations(proposalOperationsRef.current);
      if (trustedOperationRecoveryScope !== null) {
        for (const operation of discarded) {
          forgetProposalOperation(trustedOperationRecoveryScope, operation);
        }
      }
      proposalOperationTurnIdsRef.current = Object.fromEntries(
        Object.entries(proposalOperationTurnIdsRef.current).filter(
          ([key]) => !discardedKeys.has(key),
        ),
      );
      setProposalOperationTurnIds(proposalOperationTurnIdsRef.current);
    }
    if (draftTurnIdRef.current === clientTurnId) draftTurnIdRef.current = null;
    discardEmptyTrustedOperationTurn(clientTurnId);
    setError((current) => {
      if (!showRecoveryNotice) return current?.client_turn_id === clientTurnId ? null : current;
      return current?.client_turn_id === clientTurnId
        ? {
            code: "turn_cancelled_before_execution",
            message: l("已确认原请求没有执行；草稿仍在，重新发送时会使用新的安全请求编号。", "The original request did not run. Your draft remains and will use a new safe request ID when resent."),
            retryable: true,
            client_turn_id: clientTurnId,
          }
        : current;
    });
  }, [discardEmptyTrustedOperationTurn, trustedOperationRecoveryScope]);

  const updateAttachments = (updater: (current: Attachment[]) => Attachment[]) => {
    setAttachments((current) => {
      const next = updater(current);
      attachmentsRef.current = next;
      return next;
    });
  };

  const releaseImages = (items: Attachment[], keepalive = false, reportError = true) => {
    for (const item of items) {
      if (item.kind !== "image" || releasedStoredRef.current.has(item.stored)) continue;
      releasedStoredRef.current.add(item.stored);
      const url = `/api/uploads/${encodeURIComponent(item.stored)}`;
      const request = keepalive
        ? fetch(url, { method: "DELETE", headers: WRITE_HEADERS, keepalive: true })
            .then(() => undefined)
        : del<unknown>(url).then(() => undefined);
      void request.catch((reason: unknown) => {
        releasedStoredRef.current.delete(item.stored);
        if (!reportError) return;
        setError({
          code: "attachment_cleanup_failed",
          message: reason instanceof Error
            ? l(`临时附件清理失败：${reason.message}`, `Could not clean up the temporary attachment: ${reason.message}`)
            : l("临时附件清理失败", "Could not clean up the temporary attachment"),
          retryable: true,
        });
      });
    }
  };

  useEffect(() => {
    return () => {
      abortCauseRef.current = "unmount";
      uploadAbortRef.current?.abort();
      abortRef.current?.abort();
      for (const timer of proposalSettlementTimersRef.current.values()) {
        window.clearTimeout(timer);
      }
      proposalSettlementTimersRef.current.clear();
      releaseImages([
        ...attachmentsRef.current,
        ...(inFlightRef.current?.attachments ?? []),
      ], true, false);
    };
  }, []);

  const autosize = () => {
    const el = inputRef.current;
    if (!el) return;
    el.style.height = "auto";
    el.style.height = `${Math.min(el.scrollHeight, 200)}px`;
  };
  const closeNewTopicConfirmation = useCallback((restoreFocus = false) => {
    setConfirmClear(false);
    if (restoreFocus) {
      requestAnimationFrame(() => newTopicButtonRef.current?.focus());
    }
  }, []);
  useEffect(autosize, [active, input]);
  useEffect(() => {
    if (!active) {
      closeNewTopicConfirmation();
      return;
    }
    if (!confirmClear) return;
    const focusFrame = requestAnimationFrame(() => newTopicCancelRef.current?.focus());
    const dismissOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") {
        event.preventDefault();
        closeNewTopicConfirmation(true);
      }
    };
    const dismissOutside = (event: PointerEvent) => {
      const target = event.target;
      if (target instanceof Node && !newTopicControlRef.current?.contains(target)) {
        closeNewTopicConfirmation();
      }
    };
    document.addEventListener("keydown", dismissOnEscape);
    document.addEventListener("pointerdown", dismissOutside, true);
    return () => {
      cancelAnimationFrame(focusFrame);
      document.removeEventListener("keydown", dismissOnEscape);
      document.removeEventListener("pointerdown", dismissOutside, true);
    };
  }, [active, closeNewTopicConfirmation, confirmClear]);
  // Recalculate after width changes because wrapping affects the textarea height.
  useEffect(() => {
    if (!active) return;
    const el = inputRef.current;
    if (!el) return;
    const observer = new ResizeObserver(autosize);
    observer.observe(el);
    return () => observer.disconnect();
  }, [active]);

  function fillPrompt(text: string) {
    if (busyRef.current) return;
    draftTurnIdRef.current = null;
    updateReviewSupplementReference(null);
    setInput(text);
    inputRef.current?.focus();
  }

  const quickPromptPageCount = Math.max(
    1,
    Math.ceil(quickPromptRotation.length / QUICK_PROMPT_PAGE_SIZE),
  );
  const visibleQuickPrompts = Array.from(
    { length: Math.min(QUICK_PROMPT_PAGE_SIZE, quickPromptRotation.length) },
    (_, index) => quickPromptRotation[
      (quickPromptPage * QUICK_PROMPT_PAGE_SIZE + index) % quickPromptRotation.length
    ],
  );

  const promptRefreshButton = () => quickPromptPageCount > 1 && (
    <button
      type="button"
      onClick={() => setQuickPromptPage((current) => (current + 1) % quickPromptPageCount)}
      disabled={busy}
      className="chip h-8 w-8 shrink-0 justify-center !p-0"
      aria-label={l("换一组求职任务示例", "Show another set of job-search examples")}
      title={l("换一组", "Show another set")}
    >
      <svg
        viewBox="0 0 16 16"
        aria-hidden="true"
        className="h-4 w-4"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.4"
        strokeLinecap="round"
        strokeLinejoin="round"
      >
        <path d="M2.5 8a5.5 5.5 0 1 0 1.7-4M2.5 2.5V6H6" />
      </svg>
    </button>
  );

  const continueReviewRecord = useCallback((reviewReference: string) => {
    if (busyRef.current) return;
    draftTurnIdRef.current = null;
    updateReviewSupplementReference(reviewReference);
    setInput((current) => reviewSupplementComposerText(current, locale));
    globalThis.requestAnimationFrame(() => inputRef.current?.focus());
  }, [locale, updateReviewSupplementReference]);

  const settleReviewRecordProposal = useCallback((
    operationId: string,
    reviewReference: string,
    clientTurnId: string,
    retainForClarification: boolean,
  ) => {
    if (!retainForClarification && trustedOperationRecoveryScope !== null) {
      forgetReviewProposalTurn(trustedOperationRecoveryScope, clientTurnId);
    }
    // A stopped stream restores its text for editing, but once the durable Review
    // proposal settles the old turn is no longer uncertain and must never be replayed.
    if (draftTurnIdRef.current === clientTurnId) draftTurnIdRef.current = null;
    setError((current) => current?.client_turn_id === clientTurnId ? null : current);
    const boundReference = reviewSupplementReferenceRef.current;
    if (boundReference !== operationId && boundReference !== reviewReference) return;
    updateReviewSupplementReference(null);
    setInput((current) => removeReviewSupplementComposerPrompt(current));
  }, [trustedOperationRecoveryScope, updateReviewSupplementReference]);

  // New topic clears only this view, never persisted business records. Pending
  // proposals remain visible so server drafts keep a resolution path.
  async function doClear() {
    if (busyRef.current || clearingTopic) return;
    setClearingTopic(true);
    try {
      const proposalRecovery = trustedOperationRecoveryScope === null
        ? { operations: [], reviewTurnIds: [] }
        : readProposalRecovery(trustedOperationRecoveryScope);
      const knownPendingProposals = new Map<string, ProposalOperation>();
      for (const proposal of [
        ...proposalRecovery.operations,
        ...proposalOperationsRef.current,
      ]) {
        const key = proposalOperationKey(proposal);
        if (!settledProposalOperationKeysRef.current.has(key)) {
          knownPendingProposals.set(key, proposal);
        }
      }
      if (knownPendingProposals.size > 0) {
        setError({
          message: l("当前对话还有待确认方案。请先统一确认写入内容或全部不写入，再开始新话题。", "This conversation has pending proposals. Confirm what to save—or reject everything—before starting a new topic."),
        });
        setConfirmClear(false);
        return;
      }

      const ownedReviewTurnIds = new Set([
        ...trustedOperationTurnIdsRef.current,
        ...proposalRecovery.reviewTurnIds,
      ]);
      if (ownedReviewTurnIds.size > 0) {
        const turnIds = [...ownedReviewTurnIds];
        const statusResults = await Promise.allSettled(
          turnIds.map((clientTurnId) => getChatTurnStatus(clientTurnId)),
        );
        const discoveredProposals: { operation: ProposalOperation; clientTurnId: string }[] = [];
        let unsafeTurnStatus = "";
        statusResults.forEach((result, index) => {
          const clientTurnId = turnIds[index];
          if (result.status === "rejected") {
            unsafeTurnStatus = result.reason instanceof Error
              ? result.reason.message
              : l("读取失败", "Read failed");
            return;
          }
          const status = chatTurnStatusFromServer(result.value, clientTurnId);
          if (status === null) {
            unsafeTurnStatus = l("无法验证当前请求的处理状态", "the current request status could not be verified");
            return;
          }
          if (!status.terminal) {
            unsafeTurnStatus = status.state === "running"
              ? l("仍有请求正在处理", "a request is still processing")
              : l("仍有请求尚未完成", "a request has not finished");
            return;
          }
          for (const operation of status.proposalOperations) {
            if (!settledProposalOperationKeysRef.current.has(
              proposalOperationKey(operation),
            )) {
              discoveredProposals.push({ operation, clientTurnId });
            }
          }
        });
        if (unsafeTurnStatus) {
          setError({
            message: l(`暂时无法安全开始新话题：${unsafeTurnStatus}。当前对话尚未清除。`, `A new topic cannot be started safely yet: ${unsafeTurnStatus}. This conversation has not been cleared.`),
          });
          setConfirmClear(false);
          return;
        }
        if (discoveredProposals.length > 0) {
          for (const discovery of discoveredProposals) {
            revealProposalOperation(discovery.operation, discovery.clientTurnId);
          }
          setOperationRefreshSignal((current) => current + 1);
          setError({
            message: l("当前对话还有待确认方案。请先统一确认写入内容或全部不写入，再开始新话题。", "This conversation has pending proposals. Resolve them before starting a new topic."),
          });
          setConfirmClear(false);
          return;
        }

        let pendingReviewProposals: unknown;
        try {
          pendingReviewProposals = await getPendingReviewRecordConfirmations();
        } catch (reason) {
          setError({
            message: l(`暂时无法核对是否还有待确认方案：${
              reason instanceof Error ? reason.message : "读取失败"
            }。当前对话尚未清除。`, `Could not check for pending proposals: ${
              reason instanceof Error ? reason.message : "read failed"
            }. This conversation has not been cleared.`),
          });
          setConfirmClear(false);
          return;
        }
        if (!Array.isArray(pendingReviewProposals)
            || pendingReviewProposals.length > 100
            || pendingReviewProposals.some((operation) => (
              !isReviewRecordOperation(operation)
              || operation.state !== "pending_confirmation"
              || reviewRecordIntegrityIssue(operation) !== null
            ))) {
          setError({ message: l("待确认方案列表无法安全识别；当前对话尚未清除，请先重新核对。", "The pending-proposal list could not be identified safely. This conversation has not been cleared; check again first.") });
          setConfirmClear(false);
          return;
        }
        const hasPendingReviewProposal = pendingReviewProposals.some(
          (operation) => ownedReviewTurnIds.has(operation.client_turn_id),
        );
        if (hasPendingReviewProposal) {
          setError({
            message: l("当前对话还有待确认方案。请先统一确认写入内容或全部不写入，再开始新话题。", "This conversation has pending proposals. Resolve them before starting a new topic."),
          });
          setConfirmClear(false);
          return;
        }
      }

      abortCauseRef.current = "clear";
      uploadAbortRef.current?.abort();
      abortRef.current?.abort();
      releaseImages(attachmentsRef.current);
      sessionRef.current = null;
      draftTurnIdRef.current = null;
      updateReviewSupplementReference(null);
      // Pending proposals are proven empty. Retain only genuinely uncertain turns
      // for fail-closed recovery; ordinary successful receipts do not carry over.
      const retainedTrustedTurnIds = [...uncertainTrustedOperationTurnIds];
      trustedOperationTurnIdsRef.current = new Set(retainedTrustedTurnIds);
      setTrustedOperationTurnIds(retainedTrustedTurnIds);
      if (trustedOperationRecoveryScope !== null) {
        clearProposalRecovery(trustedOperationRecoveryScope);
      }
      proposalOperationsRef.current = [];
      setProposalOperations([]);
      for (const timer of proposalSettlementTimersRef.current.values()) {
        window.clearTimeout(timer);
      }
      proposalSettlementTimersRef.current.clear();
      proposalOperationTurnIdsRef.current = {};
      setProposalOperationTurnIds({});
      setTrustedOperationConversationResetSignal((current) => current + 1);
      setVisibleTrustedOperationTurnIds([]);
      resetTurnViewport(true);
      setMessages([]);
      attachmentsRef.current = [];
      setAttachments([]);
      setInput("");
      setError(null);
      setConfirmClear(false);
      if (window.innerWidth >= 768) {
        requestAnimationFrame(() => inputRef.current?.focus());
      }
    } finally {
      setClearingTopic(false);
    }
  }

  async function pickFile(file: File | null) {
    if (!file || busyRef.current || uploadAbortRef.current) return;
    if (attachmentsRef.current.length >= 8) {
      setError({ message: l("每轮最多添加 8 个附件，请先移除一个再上传。", "You can attach up to eight files per message. Remove one before uploading another.") });
      return;
    }
    const ctrl = new AbortController();
    uploadAbortRef.current = ctrl;
    setUploading(true);
    setError(null);
    try {
      const form = new FormData();
      form.append("file", file);
      const r: AttachmentUploadResponse = await uploadChatAttachment(
        form,
        { signal: ctrl.signal },
      );
      if (r.status === "error") {
        setError({ message: l("附件上传失败", "Attachment upload failed") });
      } else {
        // Invalidate the old turn only when draft attachments actually change.
        draftTurnIdRef.current = null;
        updateAttachments((current) => [...current, r]);
        if (/\.(?:xlsx|xls|csv|tsv)$/i.test(r.filename)) {
          setInput((current) => current.trim()
            ? current
            : l("请帮我批量导入这份表格中的岗位，并生成可核对的导入预览。", "Import the roles in this workbook and prepare a preview for me to review."));
          queueMicrotask(() => inputRef.current?.focus());
        }
      }
    } catch (e) {
      if (!(e instanceof DOMException && e.name === "AbortError")) {
        setError({ message: e instanceof Error ? e.message : l("附件上传失败", "Attachment upload failed") });
      }
    } finally {
      if (uploadAbortRef.current === ctrl) uploadAbortRef.current = null;
      setUploading(false);
      if (fileRef.current) fileRef.current.value = "";
    }
  }

  async function stop() {
    const inFlight = inFlightRef.current;
    if (inFlight === null || stoppingRef.current) return;
    stoppingRef.current = true;
    stopRequestedTurnIdRef.current = inFlight.clientTurnId;
    cancelAcceptedTurnIdRef.current = null;
    setStopping(true);
    setError(null);
    setToolStatus(null);
    try {
      const response = await cancelChatTurn(inFlight.clientTurnId);
      const status = chatTurnStatusFromServer(response, inFlight.clientTurnId);
      if (status === null) {
        throw new Error(l("取消结果无效", "The cancellation response is invalid"));
      }
      if (status.state === "absent") {
        throw new Error(l("取消结果缺少对应请求", "The cancellation response has no matching request"));
      }
      if (status.state === "completed" || status.state === "unknown") {
        stoppingRef.current = false;
        stopRequestedTurnIdRef.current = null;
        cancelAcceptedTurnIdRef.current = null;
        setStopping(false);
      } else if (status.state === "running" || status.state === "cancelled") {
        cancelAcceptedTurnIdRef.current = inFlight.clientTurnId;
      }
    } catch (reason) {
      if (!busyRef.current || inFlightRef.current?.clientTurnId !== inFlight.clientTurnId) return;
      stoppingRef.current = false;
      setStopping(false);
      if (reason instanceof HttpError && reason.code === "turn_finalizing") {
        stopRequestedTurnIdRef.current = null;
        cancelAcceptedTurnIdRef.current = null;
        return;
      }
      setError({
        code: "turn_cancel_failed",
        message: reason instanceof Error
          ? l(`无法取消本轮：${reason.message}`, `Could not cancel this turn: ${reason.message}`)
          : l("无法取消本轮，请重试", "Could not cancel this turn. Try again."),
        retryable: true,
        client_turn_id: inFlight.clientTurnId,
      });
    }
  }

  function removeAttachment(index: number, attachment: Attachment) {
    draftTurnIdRef.current = null;
    updateAttachments((list) => list.filter((_, current) => current !== index));
    releaseImages([attachment]);
  }

  const revealTrustedOperation = useCallback((titleElementId: string) => {
    if (!active) return;
    // Re-read current geometry after returning so stale near-bottom state cannot steal scroll.
    const root = document.documentElement;
    const nearBottom = root.scrollHeight - window.scrollY - window.innerHeight < 180;
    if (!nearBottom) return;
    document.getElementById(titleElementId)
      ?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [active]);

  const revealApplicationMergeOperation = useCallback((operationId: string) => {
    revealTrustedOperation(`application-merge-operation-title-${operationId}`);
  }, [revealTrustedOperation]);

  const revealApplicationDeleteOperation = useCallback((operationId: string) => {
    revealTrustedOperation(`application-delete-operation-title-${operationId}`);
  }, [revealTrustedOperation]);

  const revealImmediateOperation = useCallback((
    operationId: string,
    operationType: TrustedImmediateOperationType,
    clientTurnId: string,
  ) => {
    if (operationType === "review_record" && trustedOperationRecoveryScope !== null) {
      // Canonical by-turn reads also seed local recovery so a lost tool-status frame
      // cannot orphan a pending Review card after the tab closes.
      rememberReviewProposalTurn(trustedOperationRecoveryScope, clientTurnId);
    }
    let prefix: string;
    switch (operationType) {
      case "application_update":
        prefix = "application-update-operation-title";
        break;
      case "review_timeline_entry_edit":
        prefix = "review-timeline-entry-edit-operation-title";
        break;
      case "review_record":
        prefix = "review-record-operation-title";
        break;
      case "preference_update":
        prefix = "preference-update-operation-title";
        break;
      default:
        return assertNever(operationType);
    }
    revealTrustedOperation(`${prefix}-${operationId}`);
  }, [revealTrustedOperation, trustedOperationRecoveryScope]);

  async function send() {
    const message = input.trim();
    const sent = [...attachmentsRef.current];
    const sentReviewSupplementReference = reviewSupplementReferenceRef.current;
    const supplementText = sentReviewSupplementReference === null
      ? message
      : removeReviewSupplementComposerPrompt(message).trim();
    if ((!message && sent.length === 0)
        || (sentReviewSupplementReference !== null && !supplementText && sent.length === 0)
        || busyRef.current || uploadAbortRef.current
        || trustedOperationRecoveryScope === null) return;
    let requestFields: { message: string; review_supplement_reference?: string } = {
      message: message || l("（见附件）", "(see attachment)"),
    };
    if (sentReviewSupplementReference !== null) {
      try {
        requestFields = reviewSupplementRequestFields(
          supplementText,
          sentReviewSupplementReference,
          locale,
        );
      } catch (reason) {
        setError({
          message: reason instanceof Error ? reason.message : l("复盘补充请求无效", "The review follow-up request is invalid"),
        });
        return;
      }
    }
    const clientTurnId = draftTurnIdRef.current ?? makeId();
    const userMessageId = `${clientTurnId}:user`;
    const assistantMessageId = `${clientTurnId}:assistant`;
    const wasTrustedOperationCandidate = trustedOperationTurnIdsRef.current.has(clientTurnId);
    let rolledBack = false;
    let completed = false;
    let requestDispatched = false;
    let observedTrustedImmediateOperation = false;
    let knownNoExecution = false;
    let explicitlyCancelled = false;
    draftTurnIdRef.current = null;
    setConfirmClear(false);
    updateReviewSupplementReference(null);
    setInput("");
    attachmentsRef.current = [];
    setAttachments([]);
    setError(null);
    busyRef.current = true;
    setBusy(true);
    setToolStatus(null);
    abortCauseRef.current = null;
    const ctrl = new AbortController();
    abortRef.current = ctrl;
    const requestSession = sessionRef.current ?? makeId();
    sessionRef.current = requestSession;
    inFlightRef.current = { clientTurnId, attachments: sent };
    const shownText = sent.length
      ? `${message}${message ? "\n" : ""}📎 ${sent.map((a) => a.filename).join("、")}`
      : message;
    positionLatestTurn();
    setMessages((current) => [
      ...current,
      { id: userMessageId, clientTurnId, role: "user", text: shownText, uiActions: [] },
      { id: assistantMessageId, clientTurnId, role: "assistant", text: "", uiActions: [] },
    ]);

    // Roll back every non-terminal turn; partial streamed text is never saved history.
    const rollbackTurn = (reuseClientTurnId = true) => {
      if (rolledBack) return;
      rolledBack = true;
      resetTurnViewport(messages.length === 0);
      setMessages((current) => current.filter((item) => item.clientTurnId !== clientTurnId));
      setInput(message);
      updateReviewSupplementReference(sentReviewSupplementReference);
      updateAttachments((current) => [...sent, ...current]); // Preserve same-name files; no natural-key deduplication.
      draftTurnIdRef.current = reuseClientTurnId ? clientTurnId : null;
    };

    try {
      const wireAttachments = sent.map((item) => item.kind === "image"
        ? { kind: item.kind, filename: item.filename, stored: item.stored }
        : {
            kind: item.kind,
            filename: item.filename,
            text: item.text,
            truncated: item.truncated ?? false,
          });
      // Persist the minimal UUID outbox before fetch so a reload can recover the durable turn.
      if (!markTrustedImmediateOperationTurnDispatched(
        trustedOperationRecoveryScope,
        clientTurnId,
      )) {
        throw new Error(l("浏览器无法保存消息恢复信息，消息尚未发送；请检查隐私模式或存储空间后重试。", "The browser could not save message-recovery data, so your message was not sent. Check private-browsing settings or storage space and retry."));
      }
      requestDispatched = true;
      const r = await fetch("/api/chat", {
        method: "POST",
        headers: { ...WRITE_HEADERS, "Content-Type": "application/json" },
        body: JSON.stringify({
          ...requestFields,
          session_id: requestSession,
          client_turn_id: clientTurnId,
          output_locale: currentOutputLocale(),
          attachments: wireAttachments,
        }),
        signal: ctrl.signal,
      });
      if (!r.ok || !r.body) {
        const payload = await r.json().catch(() => null) as unknown;
        throw httpChatError(payload, r.status, clientTurnId);
      }
      setToolStatus(null);
      const reader = r.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let terminalError: ChatStreamError | null = null;
      try {
        readLoop: for (;;) {
          const { done, value } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          const [events, rest] = parseSse(buffer);
          buffer = rest;
          for (const ev of events) {
            if (cancelAcceptedTurnIdRef.current === clientTurnId
                && ev.event !== "done" && ev.event !== "error") {
              continue;
            }
            if (ev.event === "message_delta") {
              setToolStatus(null);
              setMessages((current) => current.map((item) => item.id === assistantMessageId
                ? { ...item, text: item.text + ev.data.text }
                : item));
            } else if (ev.event === "message_snapshot") {
              setToolStatus(null);
              setMessages((current) => current.map((item) => item.id === assistantMessageId
                ? { ...item, text: ev.data.text }
                : item));
            } else if (ev.event === "tool_status") {
              if (ev.data.tool === "record_review"
                  && trustedOperationRecoveryScope !== null) {
                // A Review proposal may persist before the turn ends. Store only its UUID
                // so the same turn can recover it without scanning global operation history.
                rememberReviewProposalTurn(trustedOperationRecoveryScope, clientTurnId);
              }
              if (ev.data.tool === "proposal_ready") {
                observedTrustedImmediateOperation = true;
                const proposal = proposalOperationFromServer(
                  ev.data.proposal_surface,
                  ev.data.proposal_operation_id,
                );
                if (proposal === null) {
                  terminalError = new ChatStreamError({
                    code: "proposal_identity_invalid",
                    message: l("无法确认这份方案是否属于当前请求，请重试以核对本轮状态。", "Could not confirm that this proposal belongs to the current request. Retry to verify the turn status."),
                    retryable: true,
                    attachments: "retained",
                    client_turn_id: clientTurnId,
                  });
                  await reader.cancel().catch(() => undefined);
                  break readLoop;
                }
                revealProposalOperation(proposal, clientTurnId);
                setToolStatus(null);
              }
              if (trustedImmediateOperationTypeFromServer(
                ev.data.trusted_operation_type,
              ) !== null) {
                observedTrustedImmediateOperation = true;
              }
              if (ev.data.tool !== "proposal_ready") {
                setToolStatus(ev.data.label);
              }
            } else if (ev.event === "done") {
              if (ev.data.client_turn_id !== clientTurnId) {
                terminalError = new ChatStreamError({
                  code: "turn_mismatch",
                  message: l("返回的结果与当前请求不匹配，请重试。", "The returned result does not match the current request. Retry."),
                  retryable: true,
                  attachments: "retained",
                  client_turn_id: clientTurnId,
                });
              } else {
                const durableProposals = proposalOperationsFromServer(
                  ev.data.proposal_operations,
                );
                if (durableProposals === null) {
                  terminalError = new ChatStreamError({
                    code: "proposal_recovery_invalid",
                    message: l("无法恢复这份待确认方案，请重试以核对本轮状态。", "Could not recover this pending proposal. Retry to verify the turn status."),
                    retryable: true,
                    attachments: "retained",
                    client_turn_id: clientTurnId,
                  });
                  await reader.cancel().catch(() => undefined);
                  break readLoop;
                }
                for (const proposal of durableProposals) {
                  revealProposalOperation(proposal, clientTurnId);
                }
                const uiActions = chatUiActionsFromServer(ev.data.ui_actions, locale);
                if (uiActions === null) {
                  setError({ message: l("助手返回的页面入口无效；正文已保留，请手动打开对应页面。", "The assistant returned an invalid page link. The response was kept; open the relevant page manually.") });
                }
                setMessages((current) => current.map((item) => item.id === assistantMessageId
                  ? { ...item, uiActions: uiActions ?? [] }
                  : item));
                observedTrustedImmediateOperation ||= durableProposals.length > 0;
                completed = true;
                sessionRef.current = ev.data.session;
                setError((current) => current?.code === "turn_cancel_failed"
                  && current.client_turn_id === clientTurnId ? null : current);
                setToolStatus(null);
              }
              await reader.cancel().catch(() => undefined);
              break readLoop;
            } else if (ev.event === "error") {
              setToolStatus(null);
              terminalError = ev.data.client_turn_id === clientTurnId
                ? new ChatStreamError({
                    ...ev.data,
                    message: localizedChatServerMessage(ev.data.code),
                  })
                : new ChatStreamError({
                    code: "turn_mismatch",
                    message: l("当前请求与返回结果不匹配，请重试。", "The current request does not match the returned result. Retry."),
                    retryable: true,
                    attachments: "retained",
                    client_turn_id: clientTurnId,
                  });
              await reader.cancel().catch(() => undefined);
              break readLoop;
            }
          }
        }
      } finally {
        reader.releaseLock();
      }
      if (terminalError) throw terminalError;
      if (!completed) throw new Error(l("连接提前中断，未收到完成确认", "The connection ended before completion was confirmed"));
    } catch (e) {
      if (!completed) {
        const cause = abortCauseRef.current;
        if (cause === "clear" || cause === "unmount") {
          releaseImages(sent, cause === "unmount", false);
        } else {
          const streamError = e instanceof ChatStreamError ? e.data : null;
          knownNoExecution = streamError?.history_committed === false;
          explicitlyCancelled = stopRequestedTurnIdRef.current === clientTurnId
            && streamError?.code === "turn_cancelled"
            && knownNoExecution;
          rollbackTurn(
            streamError?.code === "assistant_setup_failed"
              ? false
              : streamError?.retryable !== false,
          );
          if (explicitlyCancelled) {
            setError(null);
          } else {
            const messageText = e instanceof Error ? e.message : l("连接中断", "Connection interrupted");
            const recovery = streamError?.history_committed === false
              ? l("草稿和附件已恢复，可修正后重试。", "Your draft and attachments were restored. Edit them and retry.")
              : streamError?.retryable === false
                ? l("草稿和附件已恢复；请按上方提示核对现有记录，再决定是否重新发送。", "Your draft and attachments were restored. Review the existing records above before deciding whether to resend.")
                : streamError
                  ? l("草稿和附件已恢复；请稍后用同一请求重试。", "Your draft and attachments were restored. Retry the same request later.")
                  : l("草稿和附件已恢复；本轮结果尚未确认，重试会先核对原请求。", "Your draft and attachments were restored. This turn is not yet confirmed; retrying will check the original request first.");
            const separator = /[。！？!?；;]$/.test(messageText.trim()) ? "" : "；";
            setError({
              ...(streamError ?? {}),
              message: `${messageText}${separator}${recovery}`,
              client_turn_id: streamError?.client_turn_id ?? clientTurnId,
            });
          }
        }
      }
    } finally {
      if (abortRef.current === ctrl) abortRef.current = null;
      if (inFlightRef.current?.clientTurnId === clientTurnId) inFlightRef.current = null;
      if (stopRequestedTurnIdRef.current === clientTurnId) {
        stopRequestedTurnIdRef.current = null;
      }
      if (cancelAcceptedTurnIdRef.current === clientTurnId) {
        cancelAcceptedTurnIdRef.current = null;
      }
      abortCauseRef.current = null;
      busyRef.current = false;
      stoppingRef.current = false;
      setBusy(false);
      setStopping(false);
      setToolStatus(null); // Always clear transient status when a turn ends.
      // Once fetch owns a request it may have reached the server before headers arrive.
      // Conservatively recheck non-terminal turns and refresh same-ID replay candidates.
      const uncertain = requestDispatched && !completed
        && (!knownNoExecution || wasTrustedOperationCandidate);
      // Every successful turn receives one canonical by-turn check. A lost status frame
      // must not orphan a persisted proposal or receipt; terminal-empty removes pure reads.
      const keepTrustedOperationCandidate = !explicitlyCancelled && (completed
        || observedTrustedImmediateOperation
        || uncertain
        || wasTrustedOperationCandidate);
      if (requestDispatched) {
        settleTrustedImmediateOperationDispatchedTurn(
          trustedOperationRecoveryScope,
          clientTurnId,
          keepTrustedOperationCandidate,
          uncertain,
        );
      }
      if (keepTrustedOperationCandidate) {
        setVisibleTrustedOperationTurnIds((current) => (
          current.includes(clientTurnId) ? current : [...current, clientTurnId]
        ));
        setTrustedOperationTurnIds((current) => {
          trustedOperationTurnIdsRef.current.add(clientTurnId);
          if (current.includes(clientTurnId)) return current;
          const next = [...current, clientTurnId];
          return next;
        });
        setUncertainTrustedOperationTurnIds((current) => {
          if (uncertain) {
            return current.includes(clientTurnId) ? current : [...current, clientTurnId];
          }
          return current.includes(clientTurnId)
            ? current.filter((item) => item !== clientTurnId)
            : current;
        });
        setTrustedOperationRefreshSignal((current) => current + 1);
      }
      if (explicitlyCancelled) {
        discardCancelledTrustedOperationTurn(clientTurnId, false);
      }
      setOperationRefreshSignal((current) => current + 1); // Prompt proposal panels to read final state.
    }
  }

  const composer = (docked: boolean) => (
    <form
      key={docked ? "docked-chat-composer" : "empty-chat-composer"}
      ref={composerRef}
      onSubmit={(e) => {
        e.preventDefault();
        void send();
      }}
      className={
        docked
          ? "fixed bottom-0 z-20 flex flex-col gap-2 bg-gradient-to-t from-surface from-75% to-surface/0 pb-4 pt-3"
          : "flex w-full flex-col gap-2"
      }
    >
      {docked && (
        <div className="flex gap-2 overflow-x-auto pb-0.5">
          {visibleQuickPrompts.map((q) => (
            <button key={q.title} type="button" onClick={() => fillPrompt(q.text)} disabled={busy}
                    className="chip shrink-0 !py-0.5 text-xs">
              {q.title}
            </button>
          ))}
          {promptRefreshButton()}
        </div>
      )}
      {reviewSupplementReference !== null && (
        <div className="flex items-center justify-between gap-2 rounded-lg bg-info-soft px-3 py-1.5 text-xs text-info">
          <span>{l("正在补充一条复盘（可选）", "Adding optional details to a review")}</span>
          <button
            type="button"
            onClick={() => {
              draftTurnIdRef.current = null;
              updateReviewSupplementReference(null);
              setInput((current) => removeReviewSupplementComposerPrompt(current));
              inputRef.current?.focus();
            }}
            disabled={busy}
            className="font-medium underline-offset-2 hover:underline"
            aria-label={l("取消补充这条复盘", "Cancel adding to this review")}
          >
            {l("取消", "Cancel")}
          </button>
        </div>
      )}
      {attachments.length > 0 && (
        <div>
          <div className="flex flex-wrap gap-2">{attachments.map((a, i) => (
            <span key={i} className="flex items-center gap-1.5 rounded-full bg-panel-2 px-3 py-1 text-xs text-ink-2">
              <span className="text-ink-3">{a.kind === "image" ? <ImageIcon className="h-3 w-3" /> : <FileIcon className="h-3 w-3" />}</span>
              {a.filename}
              <button
                type="button"
                onClick={() => removeAttachment(i, a)}
                aria-label={l(`移除附件 ${a.filename}`, `Remove attachment ${a.filename}`)}
                className="ml-0.5 text-ink-3 hover:text-ink"
              >
                ✕
              </button>
            </span>
          ))}</div>
          {attachments.some((attachment) => /\.(?:xlsx|xls|csv|tsv)$/i.test(attachment.filename)) && (
            <p className="mt-2 text-xs leading-5 text-ink-3">
              {attachments.some(isStandardWorkbookAttachment)
                ? l("已识别 CareerDesk 标准表格，将由本地代码读取，不使用大模型；一次最多处理 200 条。", "CareerDesk workbook detected. It will be read locally without a model, up to 200 rows at a time.")
                : l("非标准表格需要由当前模型理解，可能漏掉或识别不准；一次最多处理 200 条，请核对随后出现的确认卡。", "A non-standard workbook must be interpreted by your current model and may contain omissions or mistakes. Up to 200 rows are processed at a time; review the confirmation card carefully.")}
            </p>
          )}
        </div>
      )}
      <input
        ref={fileRef}
        type="file"
        accept=".pdf,.docx,.md,.txt,.xlsx,.xls,.csv,.tsv,.png,.jpg,.jpeg,.gif,.webp"
        onChange={(e) => void pickFile(e.target.files?.[0] ?? null)}
        disabled={busy || uploading}
        className="hidden"
      />
      <div className="card flex flex-col gap-1 p-2 shadow-[var(--shadow-pop)]">
        <textarea
          ref={inputRef}
          value={input}
          onChange={(e) => {
            draftTurnIdRef.current = null;
            setInput(e.target.value);
          }}
          onKeyDown={(e) => {
            if (e.nativeEvent.isComposing) return; // Do not submit while an input method is composing text.
            if (e.key === "Enter" && !e.shiftKey) {
              e.preventDefault();
              void send();
            }
          }}
          placeholder={l("求职中遇到的任何问题，都可以在这里问我……", "Ask me anything about your job search…")}
          rows={1}
          disabled={busy}
          className="max-h-[200px] w-full resize-none bg-transparent px-2 py-1.5 text-sm leading-relaxed outline-none placeholder:text-ink-3"
        />
        <div className="flex items-center justify-between gap-2">
          <button
            type="button"
            onClick={() => fileRef.current?.click()}
            disabled={busy || uploading || attachments.length >= 8}
            title={l("上传 PDF / 文档 / 表格 / 截图", "Upload a PDF, document, workbook, or screenshot")}
            className="btn btn-sm gap-1.5 !border-transparent text-ink-3 hover:text-ink"
          >
            <svg viewBox="0 0 16 16" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
              <path d="M9.5 4 5 8.5a2 2 0 0 0 2.8 2.8l4.7-4.7a3.2 3.2 0 0 0-4.5-4.5L3.3 6.8a4.4 4.4 0 0 0 6.2 6.2L13 9.5" />
            </svg>
            {uploading ? l("上传中…", "Uploading…") : l("附件", "Attach")}
          </button>
          <div className="flex items-center gap-2.5">
            {trustedOperationRecoveryScope === null && !recoveryScopeError && (
              <span role="status" className="text-xs text-ink-3">{l("正在准备发送…", "Preparing to send…")}</span>
            )}
            <span className="hidden text-xs text-ink-3 sm:inline">{l("Enter 发送 · Shift+Enter 换行", "Enter to send · Shift+Enter for a new line")}</span>
            {busy ? (
              <button type="button" onClick={() => void stop()} disabled={stopping} title={l("取消本轮并丢弃未确认结果", "Cancel this turn and discard unconfirmed results")} aria-label={stopping ? l("正在取消", "Cancelling") : l("取消本轮", "Cancel turn")}
                      className="btn-primary h-9 w-9 shrink-0 !rounded-full !p-0">
                {stopping
                  ? <span aria-hidden className="h-4 w-4 animate-spin rounded-full border-2 border-white/40 border-t-white" />
                  : <StopIcon className="h-4 w-4" />}
              </button>
            ) : (
              <button
                disabled={trustedOperationRecoveryScope === null
                  || uploading || (!input.trim() && attachments.length === 0)}
                title={trustedOperationRecoveryScope === null
                  ? l("正在准备消息恢复", "Preparing message recovery")
                  : l("发送（Enter）", "Send (Enter)")}
                aria-label={trustedOperationRecoveryScope === null ? l("正在准备发送", "Preparing to send") : l("发送", "Send")}
                className="btn-primary h-9 w-9 shrink-0 !rounded-full !p-0"
              >
                <ArrowUpIcon className="h-4 w-4" />
              </button>
            )}
          </div>
        </div>
      </div>
    </form>
  );

  const latestUserMessageId = messages.reduce<string | null>(
    (latest, message) => message.role === "user" ? message.id : latest,
    null,
  );
  const proposalIdsFor = (surface: ProposalOperation["surface"]): string[] => (
    proposalOperations
      .filter((operation) => operation.surface === surface)
      .map((operation) => operation.operationId)
  );
  const proposalRevealHandlers: Partial<Record<
    ProposalOperation["surface"],
    (operationId: string) => void
  >> = {
    application_merge: revealApplicationMergeOperation,
    application_delete: revealApplicationDeleteOperation,
  };
  const proposalAnchorIdFor = (
    surface: ProposalOperation["surface"],
    operationId: string,
  ): string | null => {
    const clientTurnId = proposalOperationTurnIds[`${surface}:${operationId}`];
    return clientTurnId === undefined
      ? null
      : trustedImmediateOperationReceiptAnchorId(clientTurnId);
  };
  const renderError = (className = "") => error && (
    <p role="alert" className={`text-sm text-bad ${className}`.trim()}>
      {error.code === "stopped" ? l("提示", "Notice") : l("出错了", "Something went wrong")}{l("：", ": ")}{error.message}
      {(error.code === "model_not_configured"
        || error.code === "model_capabilities_missing"
        || error.code === "image_unsupported") && (
        <>
          {" "}
          <a href={SETTINGS_NEW_TAB_HREF} target="_blank" rel="noreferrer" className="underline">
            {l("在新标签页配置模型", "Configure the model in a new tab")}
          </a>
        </>
      )}
    </p>
  );
  const renderRecoveryScopeStatus = () => trustedOperationRecoveryScope === null
    && Boolean(recoveryScopeError) && (
    <div
      role={recoveryScopeError ? "alert" : "status"}
      className="flex flex-wrap items-center justify-between gap-2 rounded-xl bg-warn-soft px-3 py-2 text-sm text-warn"
    >
      <span>
        {l(`消息恢复功能暂不可用：${recoveryScopeError}。消息尚未发送。`, `Message recovery is temporarily unavailable: ${recoveryScopeError}. Your message has not been sent.`)}
      </span>
      {recoveryScopeError && (
        <button
          type="button"
          className="btn btn-sm"
          onClick={() => setRecoveryScopeRetrySignal((current) => current + 1)}
        >
          {l("重试", "Retry")}
        </button>
      )}
    </div>
  );
  const newTopicControl = active && (
    <div
      ref={newTopicControlRef}
      data-chat-new-topic-control
      className="fixed right-4 top-2.5 z-30 md:right-8 md:top-6"
    >
      <button
        ref={newTopicButtonRef}
        type="button"
        onClick={() => {
          if (!hasConversationContent) {
            inputRef.current?.focus();
            return;
          }
          setConfirmClear((current) => !current);
        }}
        disabled={busy || clearingTopic}
        aria-controls={hasConversationContent ? "chat-new-topic-confirmation" : undefined}
        aria-expanded={hasConversationContent ? confirmClear : undefined}
        title={hasConversationContent
          ? l("开始新会话（不删除后端历史、偏好、复盘或岗位记录）", "Start a new session without deleting saved history, preferences, reviews, or roles")
          : l("当前已是新话题，点击回到输入框", "This is already a new topic; return to the composer")}
        className="btn btn-sm gap-1.5 bg-panel/95 text-ink-3 shadow-[var(--shadow-card)] backdrop-blur hover:text-ink"
      >
        <svg viewBox="0 0 16 16" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
          <path d="M2.5 8a5.5 5.5 0 1 0 1.7-4M2.5 2.5V6H6" />
        </svg>
        {l("新话题", "New topic")}
      </button>
      {hasConversationContent && confirmClear && (
        <div
          id="chat-new-topic-confirmation"
          role="dialog"
          aria-labelledby="chat-new-topic-confirmation-title"
          className="absolute right-0 top-full mt-2 w-80 max-w-[calc(100vw-2rem)] rounded-2xl border border-line bg-panel/95 p-3 shadow-[var(--shadow-pop)] backdrop-blur"
        >
          <p id="chat-new-topic-confirmation-title" className="text-sm font-medium text-ink">
            {l("开始新对话？", "Start a new conversation?")}
          </p>
          <p className="mt-1 text-xs leading-relaxed text-ink-3">
            {l("当前页面会清空，已保存的历史记录和偏好不受影响。", "This view will be cleared; saved history and preferences will not be affected.")}
          </p>
          <div className="mt-3 flex justify-end gap-2">
            <button
              ref={newTopicCancelRef}
              type="button"
              onClick={() => closeNewTopicConfirmation(true)}
              disabled={clearingTopic}
              className="btn btn-sm"
            >
              {l("取消", "Cancel")}
            </button>
            <button
              type="button"
              onClick={() => void doClear()}
              disabled={busy || clearingTopic}
              className="btn btn-sm btn-danger"
            >
              {clearingTopic ? l("正在核对…", "Checking…") : l("开始新对话", "Start new conversation")}
            </button>
          </div>
        </div>
      )}
    </div>
  );

  // The empty state mounts coordinators only for operation types issued by this tab.
  // A new topic hides ordinary terminal items without orphaning pending proposals.
  return (
    <div className={hasConversationContent
      ? "mx-auto flex max-w-2xl flex-col gap-4"
      : "flex min-h-[calc(100vh-10rem)] flex-col items-center justify-center gap-7"}
    >
      {newTopicControl}
      {!hasConversationContent && (
        <div className="flex max-w-xl flex-col items-center text-center">
          <Logo className="mb-4 h-11 w-11 text-ink" />
          <h1 className="text-2xl font-semibold tracking-tight">{l("我是你的求职助手", "Your Career Assistant")}</h1>
        </div>
      )}

      {PROPOSAL_PANEL_SURFACES.map((surface) => {
        const operationIds = proposalIdsFor(surface);
        if (operationIds.length === 0) return null;
        const ProposalPanel = PROPOSAL_PANELS[surface];
        return (
          <ProposalPanel
            key={surface}
            active={active}
            refreshSignal={operationRefreshSignal}
            operationIds={operationIds}
            anchorIdForOperation={(operationId) => proposalAnchorIdFor(surface, operationId)}
            className={hasConversationContent ? "" : "w-full max-w-2xl"}
            onOperationAppeared={proposalRevealHandlers[surface]}
            onOperationSettled={(operationId) => {
              settleProposalOperation({ surface, operationId });
              if (surface === "review_undo") {
                setTrustedOperationRefreshSignal((current) => current + 1);
              }
            }}
          />
        );
      })}

      {trustedOperationRecoveryScope !== null
          && (trustedOperationTurnIds.length > 0
            || uncertainTrustedOperationTurnIds.length > 0) && (
        <TrustedImmediateOperationsPanel
          key={trustedOperationRecoveryScope}
          active={active}
          recoveryScope={trustedOperationRecoveryScope}
          clientTurnIds={trustedOperationTurnIds}
          visibleClientTurnIds={visibleTrustedOperationTurnIds}
          uncertainClientTurnIds={uncertainTrustedOperationTurnIds}
          refreshSignal={trustedOperationRefreshSignal}
          conversationResetSignal={trustedOperationConversationResetSignal}
          interactionDisabled={busy}
          className="max-w-2xl"
          receiptAnchorIdForTurn={trustedImmediateOperationReceiptAnchorId}
          onOperationAppeared={revealImmediateOperation}
          onTurnResolved={resolveTrustedOperationTurn}
          onTerminalEmptyTurn={discardEmptyTrustedOperationTurn}
          onTurnCancelled={discardCancelledTrustedOperationTurn}
          onReviewClarificationRequested={continueReviewRecord}
          onReviewProposalSettled={settleReviewRecordProposal}
          onReviewUndoPrepared={(operationId, clientTurnId) => {
            revealProposalOperation({ surface: "review_undo", operationId }, clientTurnId);
            setOperationRefreshSignal((current) => current + 1);
          }}
          onProposalOperationsDiscovered={revealDiscoveredProposalOperations}
        />
      )}

      {hasMessages && (
        <>
          <div ref={conversationLeadSpacerRef} data-chat-conversation-lead aria-hidden />
          <div ref={messagesRef} className="flex flex-col gap-3">
            {messages.map((m) =>
              m.role === "user" ? (
                <div
                  key={m.id}
                  ref={m.id === latestUserMessageId ? latestUserMessageRef : undefined}
                  data-chat-message-role="user"
                  className="max-w-[85%] self-end whitespace-pre-wrap rounded-2xl rounded-br-md bg-accent px-4 py-2.5 text-sm leading-relaxed text-accent-ink md:max-w-[75%]"
                >
                  {m.text}
                </div>
              ) : (
                <Fragment key={m.id}>
                  <div
                    data-chat-message-role="assistant"
                    className="card max-w-[88%] self-start rounded-2xl rounded-bl-md px-4 py-2.5 md:max-w-[80%]"
                  >
                  {m.text ? (
                    <>
                      <Markdown text={m.text} />
                      <ChatAssistantProgress
                        busy={busy}
                        messageId={m.id}
                        clientTurnId={inFlightRef.current?.clientTurnId ?? null}
                        label={toolStatus}
                        afterText
                      />
                    </>
                  ) : busy && m.id === `${inFlightRef.current?.clientTurnId}:assistant` ? (
                    toolStatus ? (
                      <ChatAssistantProgress
                        busy={busy}
                        messageId={m.id}
                        clientTurnId={inFlightRef.current?.clientTurnId ?? null}
                        label={toolStatus}
                      />
                    ) : (
                      <span
                        aria-label={l("助手正在生成回复", "Assistant response in progress")}
                        className="flex items-center gap-1.5 text-sm text-ink-3"
                        role="status"
                      >
                        <span aria-hidden className="inline-block h-1.5 w-1.5 animate-bounce rounded-full bg-ink-3 [animation-delay:0ms]" />
                        <span aria-hidden className="inline-block h-1.5 w-1.5 animate-bounce rounded-full bg-ink-3 [animation-delay:150ms]" />
                        <span aria-hidden className="inline-block h-1.5 w-1.5 animate-bounce rounded-full bg-ink-3 [animation-delay:300ms]" />
                      </span>
                    )
                  ) : (
                    ""
                  )}
                  {m.uiActions.length > 0 && (
                    <div className="mt-3 flex flex-wrap gap-2 border-t border-line pt-3">
                      {m.uiActions.map((action) => (
                        <Link
                          className="btn-secondary chat-ui-action"
                          key={`${action.kind}:${action.resourceId ?? ""}`}
                          to={action.href}
                        >
                          {action.label}
                          <span aria-hidden="true">→</span>
                        </Link>
                      ))}
                    </div>
                  )}
                  </div>
                  <div
                    id={trustedImmediateOperationReceiptAnchorId(m.clientTurnId)}
                    className="flex w-full flex-col gap-1.5"
                  />
                </Fragment>
              ),
            )}
          </div>
        </>
      )}

      {hasConversationContent ? (
        <Fragment key="conversation-chat-layout">
          <div ref={turnSpacerRef} data-chat-turn-spacer aria-hidden />
          {renderRecoveryScopeStatus()}
          {renderError()}
          <div ref={composerReservationRef} data-chat-composer-reservation aria-hidden />
          {composer(true)}
        </Fragment>
      ) : (
        <Fragment key="empty-chat-layout">
          <div className="w-full max-w-2xl">
            {renderRecoveryScopeStatus()}
            {composer(false)}
            {renderError("mt-2")}
          </div>
          <div className="w-full max-w-3xl" aria-label={l("常用求职任务", "Common job-search tasks")}>
            <div
              className="flex flex-wrap items-center justify-center gap-2"
              aria-label={l(`第 ${quickPromptPage + 1} 组求职任务示例`, `Job-search example set ${quickPromptPage + 1}`)}
            >
              {visibleQuickPrompts.map((q) => (
                <button key={q.title} type="button" onClick={() => fillPrompt(q.text)} title={q.hint}
                        disabled={busy} className="chip !gap-1.5 text-xs">
                  <span className="text-ink-3"><PromptIconGlyph name={q.icon} /></span>
                  {q.title}
                </button>
              ))}
              {promptRefreshButton()}
            </div>
          </div>
        </Fragment>
      )}
    </div>
  );
}
