import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import { HttpError } from "../../shared/api/transport";
import type { UiLocale } from "../../i18n/i18n";
import { useLocale } from "../../i18n/localePreference";
import { useLocalizer } from "../../i18n/useLocalizer";

import {
  getApplicationUpdateOperation,
  getApplicationUpdateOperationsByClientTurn,
  getApplicationUpdateUndoCommandStatus,
  undoApplicationUpdateOperation,
} from "../application-update-operations/applicationUpdateOperationApi";
import type {
  ApplicationUpdateOperation,
  ApplicationUpdateUndoCommandStatus,
} from "../application-update-operations/applicationUpdateOperationContract";
import {
  getReviewTimelineEntryEditOperation,
  getReviewTimelineEntryEditOperationsByClientTurn,
  getReviewTimelineEntryEditUndoCommandStatus,
  undoReviewTimelineEntryEditOperation,
} from "../review-timeline-entry-edit-operations/reviewTimelineEntryEditOperationApi";
import type {
  ReviewTimelineEntryEditOperation,
  ReviewTimelineEntryEditUndoCommandStatus,
} from "../review-timeline-entry-edit-operations/reviewTimelineEntryEditOperationContract";
import {
  decideReviewRecordOperationsByClientTurn,
  getPendingReviewRecordConfirmations,
  getPendingReviewRecordClarifications,
  getReviewRecordOperation,
  getReviewRecordOperationsByClientTurn,
  prepareReviewRecordUndoOperation,
} from "../review-record-operations/reviewRecordOperationApi";
import type {
  ReviewRecordOperation,
} from "../review-record-operations/reviewRecordOperationContract";
import {
  cancelChatTurnIfAbsent,
  getChatTurnStatus,
} from "../chat/chatApi";
import type { ChatTurnStatus } from "../chat/chatContract";
import {
  proposalOperationFromServer,
  type ProposalOperation,
} from "../chat/chatProposalRecovery";
import {
  applicationUpdateOperationAnnouncement,
} from "../application-update-operations/applicationUpdateOperationPresentation";
import {
  applicationUpdateOperationIntegrityIssue,
  isApplicationUpdateOperation,
  isApplicationUpdateUndoCommandStatus,
} from "../application-update-operations/applicationUpdateOperationContract";
import {
  getPreferenceUpdateOperationsByClientTurn,
} from "../preference-operations/preferenceOperationApi";
import {
  isPreferenceUpdateOperation,
  preferenceOperationIntegrityIssue,
  type PreferenceUpdateOperation,
} from "../preference-operations/preferenceOperationContract";
import {
  preferenceOperationAnnouncement,
} from "../preference-operations/preferenceOperationPresentation";
import {
  reviewTimelineEntryEditOperationAnnouncement,
} from "../review-timeline-entry-edit-operations/reviewTimelineEntryEditOperationPresentation";
import {
  isReviewTimelineEntryEditOperation,
  isReviewTimelineEntryEditUndoCommandStatus,
  reviewTimelineEntryEditOperationIntegrityIssue,
} from "../review-timeline-entry-edit-operations/reviewTimelineEntryEditOperationContract";
import { ReviewRecordProposalBatchCard } from "../review-record-operations/ReviewRecordProposalBatchCard";
import {
  groupReviewRecordProposalsByTurn,
  type ReviewRecordProposalBatchDecision,
} from "../review-record-operations/reviewRecordProposalBatch";
import {
  REVIEW_RECORD_TARGET_STATE_LABELS,
  isPreparedReviewUndoOperation,
  isReviewRecordOperation,
  reviewRecordIntegrityIssue,
} from "../review-record-operations/reviewRecordOperationContract";
import {
  reconcilePendingClarificationSnapshot,
  shouldFreezeRetainedClarifications,
} from "../review-record-operations/reconcilePendingClarifications";
import {
  MAX_TRUSTED_IMMEDIATE_OPERATION_RECOVERY_TURNS,
  readTrustedImmediateOperationOutbox,
  selectRetainedRecoveryTurnIds,
  storeUncertainTrustedImmediateOperationActions,
  type TrustedImmediateOperationActionCommand,
  type ActionOperationType,
  type TrustedImmediateOperationType,
} from "./trustedImmediateOperationOutbox";
import {
  classifyTrustedImmediateOperationTurn,
  countMissingKnownTrustedImmediateOperations,
  indexKnownTrustedImmediateOperations,
  loadTrustedImmediateOperationTurn,
} from "./trustedImmediateOperationRecovery";
import { createSecureCommandId } from "./secureCommandId";

type TrustedActionOperation = ApplicationUpdateOperation | ReviewTimelineEntryEditOperation;
type TrustedImmediateOperation =
  | TrustedActionOperation
  | ReviewRecordOperation
  | PreferenceUpdateOperation;
type TrustedImmediateUndoCommandStatus =
  | ApplicationUpdateUndoCommandStatus
  | ReviewTimelineEntryEditUndoCommandStatus;
type ProposalOperationDiscovery = {
  operation: ProposalOperation;
  clientTurnId: string;
};

const VALID_CHAT_TURN_STATES = new Set([
  "absent", "running", "completed", "unknown", "cancelled",
]);
const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;
const UNCERTAIN_RETRY_DELAYS_MS = [1_000, 2_000, 4_000, 8_000, 15_000, 30_000] as const;
const UNCERTAIN_HEARTBEAT_MS = 60_000;
const UNDO_RETRY_DELAYS_MS = [1_000, 2_000, 4_000, 8_000, 15_000, 30_000] as const;
const REVIEW_RECORD_PROPOSAL_RECOVERY_MS = 15_000;
const MAX_REVIEW_RECORD_RECEIPTS_PER_TURN = 50;

export function trustedImmediateOperationReceiptAnchorId(clientTurnId: string): string {
  return `trusted-immediate-operation-receipts-${clientTurnId}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function assertNever(value: never): never {
  throw new Error(`Unhandled operation type: ${String(value)}`);
}

function isUuid(value: unknown): value is string {
  return typeof value === "string" && UUID_PATTERN.test(value);
}

function sameStrings(left: string[], right: string[]): boolean {
  return left.length === right.length && left.every((item, index) => item === right[index]);
}

function isTrustedImmediateOperation(value: unknown): value is TrustedImmediateOperation {
  if (!isRecord(value)) return false;
  switch (value.operation_type) {
    case "application_update":
      return isApplicationUpdateOperation(value);
    case "review_timeline_entry_edit":
      return isReviewTimelineEntryEditOperation(value);
    case "review_record":
      return isReviewRecordOperation(value);
    case "preference_update":
      return isPreferenceUpdateOperation(value);
    default:
      return false;
  }
}

function isTrustedActionOperation(
  operation: TrustedImmediateOperation,
): operation is TrustedActionOperation {
  switch (operation.operation_type) {
    case "application_update":
    case "review_timeline_entry_edit":
      return true;
    case "review_record":
    case "preference_update":
      return false;
    default:
      return assertNever(operation);
  }
}

function isChatTurnStatus(
  value: unknown,
  expectedClientTurnId: string,
): value is ChatTurnStatus {
  if (!isRecord(value)
      || value.client_turn_id !== expectedClientTurnId
      || !isUuid(value.client_turn_id)
      || typeof value.state !== "string"
      || !VALID_CHAT_TURN_STATES.has(value.state)
      || typeof value.terminal !== "boolean"
      || !Array.isArray(value.proposal_operations)
      || value.proposal_operations.length > 200
      || value.proposal_operations.some((item) => (
        !isRecord(item)
        || proposalOperationFromServer(item.surface, item.operation_id) === null
      ))) return false;
  const proposalKeys = value.proposal_operations.map((item) => (
    `${String(item.surface)}:${String(item.operation_id)}`
  ));
  if (new Set(proposalKeys).size !== proposalKeys.length) return false;
  return value.terminal === (
    value.state === "completed" || value.state === "unknown" || value.state === "cancelled"
  );
}

function isTrustedImmediateUndoCommandStatus(
  value: unknown,
  expectedCommandId: string,
  expectedOperationId: string,
  operationType: ActionOperationType,
): value is TrustedImmediateUndoCommandStatus {
  switch (operationType) {
    case "application_update":
      return isApplicationUpdateUndoCommandStatus(value, expectedCommandId, expectedOperationId);
    case "review_timeline_entry_edit":
      return isReviewTimelineEntryEditUndoCommandStatus(
        value,
        expectedCommandId,
        expectedOperationId,
      );
    default:
      return assertNever(operationType);
  }
}

function trustedOperationIntegrityIssue(operation: TrustedImmediateOperation): string | null {
  switch (operation.operation_type) {
    case "application_update":
      return applicationUpdateOperationIntegrityIssue(operation);
    case "review_timeline_entry_edit":
      return reviewTimelineEntryEditOperationIntegrityIssue(operation);
    case "review_record":
      return reviewRecordIntegrityIssue(operation);
    case "preference_update":
      return preferenceOperationIntegrityIssue(operation);
    default:
      return assertNever(operation);
  }
}

function waitForRetry(delayMs: number, signal: AbortSignal): Promise<boolean> {
  if (signal.aborted) return Promise.resolve(false);
  return new Promise((resolve) => {
    const timer = window.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve(true);
    }, delayMs);
    const onAbort = () => {
      window.clearTimeout(timer);
      resolve(false);
    };
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

type OperationFamilyAdapter = {
  operationType: TrustedImmediateOperationType;
  label: Readonly<{ zhCN: string; en: string }>;
  maxReceiptsPerTurn?: number;
  loadByTurn: (
    clientTurnId: string,
    init: Pick<RequestInit, "signal">,
  ) => Promise<unknown>;
  validate: (value: unknown) => value is TrustedImmediateOperation;
};

const MAX_APPLICATION_UPDATE_RECEIPTS_PER_TURN = 20;

const OPERATION_FAMILY_ADAPTERS: readonly OperationFamilyAdapter[] = [
  {
    operationType: "application_update",
    label: { zhCN: "岗位修改", en: "role update" },
    maxReceiptsPerTurn: MAX_APPLICATION_UPDATE_RECEIPTS_PER_TURN,
    loadByTurn: getApplicationUpdateOperationsByClientTurn,
    validate: isApplicationUpdateOperation,
  },
  {
    operationType: "review_timeline_entry_edit",
    label: { zhCN: "复盘历程编辑", en: "review history edit" },
    loadByTurn: getReviewTimelineEntryEditOperationsByClientTurn,
    validate: isReviewTimelineEntryEditOperation,
  },
  {
    operationType: "review_record",
    label: { zhCN: "复盘记录", en: "review record" },
    maxReceiptsPerTurn: MAX_REVIEW_RECORD_RECEIPTS_PER_TURN,
    loadByTurn: getReviewRecordOperationsByClientTurn,
    validate: isReviewRecordOperation,
  },
  {
    operationType: "preference_update",
    label: { zhCN: "偏好更新", en: "preference update" },
    maxReceiptsPerTurn: 1,
    loadByTurn: getPreferenceUpdateOperationsByClientTurn,
    validate: isPreferenceUpdateOperation,
  },
];

type ActionOperationAdapter = {
  submitUndo: (
    operationId: string,
    commandId: string,
    init: Pick<RequestInit, "signal">,
  ) => Promise<unknown>;
  loadCommandStatus: (
    commandId: string,
    init: Pick<RequestInit, "signal">,
  ) => Promise<unknown>;
  loadCanonical: (
    operationId: string,
    init: Pick<RequestInit, "signal">,
  ) => Promise<unknown>;
};

const ACTION_OPERATION_ADAPTERS: Record<ActionOperationType, ActionOperationAdapter> = {
  application_update: {
    submitUndo: undoApplicationUpdateOperation,
    loadCommandStatus: getApplicationUpdateUndoCommandStatus,
    loadCanonical: getApplicationUpdateOperation,
  },
  review_timeline_entry_edit: {
    submitUndo: undoReviewTimelineEntryEditOperation,
    loadCommandStatus: getReviewTimelineEntryEditUndoCommandStatus,
    loadCanonical: getReviewTimelineEntryEditOperation,
  },
};

function trustedOperationAnnouncement(
  operation: TrustedImmediateOperation,
  actionUncertain: boolean,
  locale: UiLocale = "zh-CN",
): string {
  const en = locale === "en";
  switch (operation.operation_type) {
    case "application_update":
      return applicationUpdateOperationAnnouncement(operation, actionUncertain, locale);
    case "review_timeline_entry_edit":
      return reviewTimelineEntryEditOperationAnnouncement(operation, actionUncertain, locale);
    case "review_record":
      if (reviewRecordIntegrityIssue(operation) !== null) return en ? "Review result needs verification" : "复盘结果异常，需要核对";
      {
        const state: ReviewRecordOperation["state"] = operation.state;
        switch (state) {
        case "processing":
          return en ? "Review source saved; extracting" : "复盘原文已保存，正在提取";
        case "pending_confirmation":
          return en ? "Review proposal awaits confirmation or rejection" : "复盘方案等待勾选或放弃";
        case "completed":
          if (operation.outcome !== "applied") return en ? "Original review saved; more details needed" : "复盘原话已保存，等待补充";
          if (operation.undo_block_reason === "target_not_applied") {
            return en ? "Review was recorded; its target is no longer applied" : `复盘曾记录，当前${REVIEW_RECORD_TARGET_STATE_LABELS[operation.target_current_state]}`;
          }
          return operation.undo_block_reason === "target_changed"
            ? en ? "Review recorded; updated later" : "复盘已记录，后来又有更新"
            : en ? "Review recorded" : "复盘已经记录";
        case "failed":
          return en ? "Review was not published to the tracker or question library" : "复盘未发布到看板或题库";
        case "superseded":
          return en ? "A newer supplement replaced this review process" : "旧的复盘处理已被更新补充取代";
        case "rejected":
          return en ? "Review proposal rejected" : "复盘方案已放弃";
        default:
          return assertNever(state);
        }
      }
    case "preference_update":
      return preferenceOperationAnnouncement(operation, locale);
    default:
      return assertNever(operation);
  }
}

type ReviewRecordProposalNotice = {
  message: string;
  tone: "settled" | "uncertain";
  clientTurnId: string;
};

type ReviewRecordProposalNoticeGroup = ReviewRecordProposalNotice & {
  operationIds: string[];
};

function isBatchableSettledReviewRecord(
  operation: TrustedImmediateOperation,
): operation is ReviewRecordOperation {
  return operation.operation_type === "review_record"
    && (operation.state === "rejected"
      || (operation.state === "completed" && operation.outcome === "applied"));
}

type UndoableReviewRecord = ReviewRecordOperation & {
  state: "completed";
  outcome: "applied";
  result: NonNullable<ReviewRecordOperation["result"]>;
};

function isUndoableReviewRecord(value: unknown): value is UndoableReviewRecord {
  return isReviewRecordOperation(value)
    && value.state === "completed"
    && value.outcome === "applied"
    && value.result?.outcome === "applied"
    && value.undo_available
    && reviewRecordIntegrityIssue(value) === null;
}

function reviewRecordBatchSummary(operations: readonly ReviewRecordOperation[], locale: UiLocale = "zh-CN"): string {
  const en = locale === "en";
  const appliedCount = operations.filter((operation) => (
    operation.state === "completed" && operation.outcome === "applied"
  )).length;
  const rejectedCount = operations.filter((operation) => operation.state === "rejected").length;
  if (appliedCount === operations.length) {
    return en ? `Updated ${appliedCount} application and review records as confirmed.` : `已按确认内容更新 ${appliedCount} 条求职进展和复盘记录。`;
  }
  if (rejectedCount === operations.length) {
    return en ? `Rejected all ${rejectedCount} review proposals; no role records were written.` : `已放弃本批 ${rejectedCount} 条复盘方案，未写入任何岗位记录。`;
  }
  return en ? `Processed ${operations.length} review proposals: applied ${appliedCount}, rejected ${rejectedCount}.` : `本批 ${operations.length} 条复盘方案已处理：写入 ${appliedCount} 条，放弃 ${rejectedCount} 条。`;
}

function reviewRecordProposalOutcomeNotice(
  operation: ReviewRecordOperation,
  locale: UiLocale = "zh-CN",
): ReviewRecordProposalNotice | null {
  const en = locale === "en";
  if (operation.state === "rejected") {
    return {
      message: en ? "Rejected this review proposal; no role records were written." : "已放弃这条复盘方案，未写入任何岗位记录。",
      tone: "settled",
      clientTurnId: operation.client_turn_id,
    };
  }
  if (operation.state !== "completed") return null;
  return {
    message: operation.outcome === "applied"
      ? en ? "Updated the application and review records as confirmed." : "已按确认内容更新求职进展和复盘记录。"
      : en ? "Kept this review draft without updating the tracker or question library." : "已保留这条复盘草稿，未更新求职进展或题库。",
    tone: "settled",
    clientTurnId: operation.client_turn_id,
  };
}

function reviewRecordProposalChangedElsewhereNotice(
  clientTurnId: string,
  locale: UiLocale = "zh-CN",
): ReviewRecordProposalNotice {
  return {
    message: locale === "en" ? "This review proposal changed in another window or a later supplement. This page cannot confirm the exact outcome; use the latest role record as authoritative." : "这份复盘方案已在其它窗口或后续补充中发生变化；当前页面无法确认具体结果，请以最新岗位记录为准。",
    tone: "uncertain",
    clientTurnId,
  };
}

function retainReviewTurnForClarification(operation: ReviewRecordOperation): boolean {
  return operation.state === "completed"
    && operation.outcome === "needs_clarification"
    && operation.result?.outcome === "needs_clarification";
}

function retainReviewTurnAfterSettlement(
  operations: Iterable<TrustedImmediateOperation>,
  settled: ReviewRecordOperation,
): boolean {
  return [...operations].some((operation) => (
    operation.operation_type === "review_record"
    && operation.client_turn_id === settled.client_turn_id
    && operation.operation_id !== settled.operation_id
    && (operation.state === "processing"
      || operation.state === "pending_confirmation"
      || retainReviewTurnForClarification(operation))
  )) || retainReviewTurnForClarification(settled);
}

export function TrustedImmediateOperationsPanel({
  active,
  recoveryScope,
  clientTurnIds,
  visibleClientTurnIds,
  uncertainClientTurnIds,
  refreshSignal,
  conversationResetSignal,
  interactionDisabled = false,
  className = "",
  receiptAnchorIdForTurn,
  onOperationAppeared,
  onTurnResolved,
  onTerminalEmptyTurn,
  onTurnCancelled,
  onReviewClarificationRequested,
  onReviewProposalSettled,
  onReviewUndoPrepared,
  onProposalOperationsDiscovered,
}: {
  active: boolean;
  recoveryScope: string;
  clientTurnIds: string[];
  visibleClientTurnIds: string[];
  uncertainClientTurnIds: string[];
  refreshSignal: number;
  conversationResetSignal: number;
  interactionDisabled?: boolean;
  className?: string;
  receiptAnchorIdForTurn?: (clientTurnId: string) => string;
  onOperationAppeared?: (
    operationId: string,
    operationType: TrustedImmediateOperationType,
    clientTurnId: string,
  ) => void;
  onTurnResolved?: (clientTurnId: string) => void;
  onTerminalEmptyTurn?: (clientTurnId: string) => void;
  onTurnCancelled?: (clientTurnId: string) => void;
  onReviewClarificationRequested?: (reviewReference: string) => void;
  onReviewProposalSettled?: (
    operationId: string,
    reviewReference: string,
    clientTurnId: string,
    retainForClarification: boolean,
  ) => void;
  onReviewUndoPrepared?: (operationId: string, clientTurnId: string) => void;
  onProposalOperationsDiscovered?: (discoveries: ProposalOperationDiscovery[]) => void;
}) {
  const l = useLocalizer();
  const { locale } = useLocale();
  const [initialOutbox] = useState(() => readTrustedImmediateOperationOutbox(recoveryScope));
  const [protectedActionTurnIds, setProtectedActionTurnIds] = useState<string[]>(
    () => [...new Set(initialOutbox.uncertainActionCommands.map((item) => item.clientTurnId))],
  );
  const uniqueClientTurnIds = useMemo(() => selectRetainedRecoveryTurnIds(
    [...new Set(clientTurnIds)],
    new Set(protectedActionTurnIds),
    new Set(uncertainClientTurnIds),
    MAX_TRUSTED_IMMEDIATE_OPERATION_RECOVERY_TURNS,
  ), [clientTurnIds, protectedActionTurnIds, uncertainClientTurnIds]);
  const clientTurnKey = uniqueClientTurnIds.join("\u0000");
  const visibleClientTurnIdSet = useMemo(
    () => new Set(visibleClientTurnIds),
    [visibleClientTurnIds],
  );
  const externalUncertainTurnIds = useMemo(
    () => new Set(uncertainClientTurnIds),
    [uncertainClientTurnIds],
  );
  const externalUncertainTurnKey = [...externalUncertainTurnIds].sort().join("\u0000");
  const clientTurnIdsRef = useRef(uniqueClientTurnIds);
  clientTurnIdsRef.current = uniqueClientTurnIds;

  const [operations, setOperations] = useState<TrustedImmediateOperation[]>([]);
  const [operationErrors, setOperationErrors] = useState<Record<string, string>>({});
  const [uncertainTurnIds, setUncertainTurnIds] = useState<Set<string>>(() => new Set());
  const [uncertainActionOperationIds, setUncertainActionOperationIds] = useState<Set<string>>(
    () => new Set(initialOutbox.uncertainActionCommands.map((item) => item.operationId)),
  );
  const [pendingRecoveryTurnIds, setPendingRecoveryTurnIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [cancellableAbsentTurnIds, setCancellableAbsentTurnIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [listError, setListError] = useState("");
  const [loading, setLoading] = useState(false);
  const [cancellingRecovery, setCancellingRecovery] = useState(false);
  const [recoveryCancelError, setRecoveryCancelError] = useState("");
  const [actionOperationId, setActionOperationId] = useState<string | null>(null);
  const [preparingReviewRecordOperationId, setPreparingReviewRecordOperationId] = useState<
    string | null
  >(null);
  const [reviewRecordProposalAction, setReviewRecordProposalAction] = useState<{
    clientTurnId: string;
    action: "approve" | "reject";
  } | null>(null);
  const [pendingClarificationOperationIds, setPendingClarificationOperationIds] = useState<
    Set<string>
  >(() => new Set());
  const [pendingClarificationReadUncertain, setPendingClarificationReadUncertain] = useState(false);
  const [uncertainReviewRecordProposalIds, setUncertainReviewRecordProposalIds] = useState<
    Set<string>
  >(() => new Set());
  const [reviewRecordNotices, setReviewRecordNotices] = useState<Record<string, string>>({});
  const [reviewProposalOutcomeNotices, setReviewProposalOutcomeNotices] = useState<
    Record<string, ReviewRecordProposalNotice>
  >({});
  const [visibilitySignal, setVisibilitySignal] = useState(0);
  const [manualRefreshSignal, setManualRefreshSignal] = useState(0);
  const [autoRetryRound, setAutoRetryRound] = useState(0);
  const pendingRecoveryTurnKey = [...pendingRecoveryTurnIds].sort().join("\u0000");

  const operationsRef = useRef<TrustedImmediateOperation[]>([]);
  const mountedRef = useRef(true);
  const loadAbortRef = useRef<AbortController | null>(null);
  const actionAbortRef = useRef<AbortController | null>(null);
  const reviewRecordActionAbortRef = useRef<AbortController | null>(null);
  const recoveryCancelAbortRef = useRef<AbortController | null>(null);
  const actionOperationIdRef = useRef<string | null>(null);
  const reviewRecordProposalActionRef = useRef<string | null>(null);
  const requestEpochRef = useRef(0);
  const seenOperationIdsRef = useRef<Set<string>>(new Set());
  const uncertainActionOperationIdsRef = useRef<Set<string>>(
    new Set(initialOutbox.uncertainActionCommands.map((item) => item.operationId)),
  );
  const pendingClarificationOperationIdsRef = useRef<Set<string>>(new Set());
  const pendingConfirmationOperationIdsRef = useRef<Set<string>>(new Set());
  const uncertainReviewRecordProposalIdsRef = useRef<Set<string>>(new Set());
  const reviewRecordProposalRecoveryActionsRef = useRef<Map<
    string,
    ReviewRecordProposalBatchDecision
  >>(new Map());
  const reviewProposalNoticeTimersRef = useRef(new Map<string, number>());
  const resumePersistedActionIdsRef = useRef<Set<string>>(
    new Set(initialOutbox.uncertainActionCommands.map((item) => item.operationId)),
  );
  const actionCommandIdsRef = useRef<Map<string, {
    operationType: ActionOperationType;
    commandId: string;
    clientTurnId: string;
  }>>(
    new Map(initialOutbox.uncertainActionCommands.map((item) => [
      item.operationId,
      {
        operationType: item.operationType,
        commandId: item.commandId,
        clientTurnId: item.clientTurnId,
      },
    ])),
  );

  const updateUncertainActionOperationIds = useCallback(
    (updater: (current: Set<string>) => Set<string>) => {
      const next = updater(uncertainActionOperationIdsRef.current);
      const persistedCommands: TrustedImmediateOperationActionCommand[] = [...next].flatMap(
        (operationId) => {
        const command = actionCommandIdsRef.current.get(operationId);
        return command ? [{ operationId, ...command }] : [];
        },
      );
      if (!storeUncertainTrustedImmediateOperationActions(recoveryScope, persistedCommands)) {
        return false;
      }
      uncertainActionOperationIdsRef.current = next;
      for (const operationId of actionCommandIdsRef.current.keys()) {
        if (!next.has(operationId)) actionCommandIdsRef.current.delete(operationId);
      }
      const nextProtectedTurnIds = [
        ...new Set(persistedCommands.map((item) => item.clientTurnId)),
      ];
      setProtectedActionTurnIds((current) => (
        sameStrings(current, nextProtectedTurnIds) ? current : nextProtectedTurnIds
      ));
      setUncertainActionOperationIds(next);
      return true;
    },
    [recoveryScope],
  );

  const storeReviewProposalOutcomeNotice = useCallback((
    operationId: string,
    notice: ReviewRecordProposalNotice,
  ) => {
    const previousTimer = reviewProposalNoticeTimersRef.current.get(operationId);
    if (previousTimer !== undefined) window.clearTimeout(previousTimer);
    setReviewProposalOutcomeNotices((current) => ({
      ...current,
      [operationId]: notice,
    }));
    const timer = window.setTimeout(() => {
      reviewProposalNoticeTimersRef.current.delete(operationId);
      if (!mountedRef.current) return;
      setReviewProposalOutcomeNotices((current) => {
        if (!(operationId in current)) return current;
        const next = { ...current };
        delete next[operationId];
        return next;
      });
    }, 2000);
    reviewProposalNoticeTimersRef.current.set(operationId, timer);
  }, []);

  const replaceOperations = useCallback((next: TrustedImmediateOperation[]) => {
    const sorted = [...next].sort((left, right) => {
      const byTime = Date.parse(left.created_time) - Date.parse(right.created_time);
      return Number.isNaN(byTime) || byTime === 0
        ? left.operation_id.localeCompare(right.operation_id)
        : byTime;
    });
    operationsRef.current = sorted;
    setOperations(sorted);
  }, []);

  const updateUncertainReviewRecordProposalIds = useCallback(
    (updater: (current: Set<string>) => Set<string>) => {
      const next = updater(uncertainReviewRecordProposalIdsRef.current);
      uncertainReviewRecordProposalIdsRef.current = next;
      setUncertainReviewRecordProposalIds(next);
    },
    [],
  );

  useEffect(() => {
    const allowedTurnIds = new Set(uniqueClientTurnIds);
    const retained = operationsRef.current.filter((operation) => (
      allowedTurnIds.has(operation.client_turn_id)
      && (operation.state === "processing"
        || uncertainActionOperationIdsRef.current.has(operation.operation_id)
        || uncertainReviewRecordProposalIdsRef.current.has(operation.operation_id)
        || (operation.operation_type === "review_record"
          && (operation.state === "pending_confirmation"
            || pendingClarificationOperationIdsRef.current.has(operation.operation_id))))
    ));
    const retainedIds = new Set(retained.map((operation) => operation.operation_id));
    replaceOperations(retained);
    setOperationErrors((current) => Object.fromEntries(
      Object.entries(current).filter(([operationId]) => retainedIds.has(operationId)),
    ));
    setReviewRecordNotices({});
    setReviewProposalOutcomeNotices({});
    for (const timer of reviewProposalNoticeTimersRef.current.values()) {
      window.clearTimeout(timer);
    }
    reviewProposalNoticeTimersRef.current.clear();
  }, [conversationResetSignal, replaceOperations, uniqueClientTurnIds]);

  useEffect(() => {
    mountedRef.current = true;
    const onVisibilityChange = () => {
      if (document.hidden) {
        requestEpochRef.current += 1;
        loadAbortRef.current?.abort();
        reviewRecordActionAbortRef.current?.abort();
      } else {
        setAutoRetryRound(0);
      }
      setVisibilitySignal((current) => current + 1);
    };
    document.addEventListener("visibilitychange", onVisibilityChange);
    return () => {
      mountedRef.current = false;
      requestEpochRef.current += 1;
      loadAbortRef.current?.abort();
      actionAbortRef.current?.abort();
      reviewRecordActionAbortRef.current?.abort();
      recoveryCancelAbortRef.current?.abort();
      for (const timer of reviewProposalNoticeTimersRef.current.values()) {
        window.clearTimeout(timer);
      }
      reviewProposalNoticeTimersRef.current.clear();
      document.removeEventListener("visibilitychange", onVisibilityChange);
    };
  }, []);

  useEffect(() => {
    if (uniqueClientTurnIds.length !== 0) return;
    const retainedOperations = operationsRef.current.filter((operation) => (
      operation.operation_type === "review_record"
      && (pendingClarificationOperationIdsRef.current.has(operation.operation_id)
        || pendingConfirmationOperationIdsRef.current.has(operation.operation_id)
        || uncertainReviewRecordProposalIdsRef.current.has(operation.operation_id))
    ));
    const retainedOperationIds = new Set(
      retainedOperations.map((operation) => operation.operation_id),
    );
    const retainedClientTurnIds = new Set(
      retainedOperations.map((operation) => operation.client_turn_id),
    );
    requestEpochRef.current += 1;
    loadAbortRef.current?.abort();
    actionAbortRef.current?.abort();
    actionOperationIdRef.current = null;
    setActionOperationId(null);
    if (reviewRecordProposalActionRef.current !== null
        && !retainedClientTurnIds.has(reviewRecordProposalActionRef.current)) {
      reviewRecordProposalActionRef.current = null;
      setReviewRecordProposalAction(null);
    }
    replaceOperations(retainedOperations);
    setOperationErrors({});
    setUncertainTurnIds(new Set());
    uncertainActionOperationIdsRef.current = new Set();
    resumePersistedActionIdsRef.current = new Set();
    actionCommandIdsRef.current = new Map();
    storeUncertainTrustedImmediateOperationActions(recoveryScope, []);
    setProtectedActionTurnIds((current) => (current.length === 0 ? current : []));
    setUncertainActionOperationIds(new Set());
    setPendingRecoveryTurnIds(new Set());
    setCancellableAbsentTurnIds(new Set());
    const retainedUncertainProposalIds = new Set(
      [...uncertainReviewRecordProposalIdsRef.current].filter((operationId) => (
        retainedOperationIds.has(operationId)
      )),
    );
    uncertainReviewRecordProposalIdsRef.current = retainedUncertainProposalIds;
    for (const operationId of reviewRecordProposalRecoveryActionsRef.current.keys()) {
      if (!retainedUncertainProposalIds.has(operationId)) {
        reviewRecordProposalRecoveryActionsRef.current.delete(operationId);
      }
    }
    setUncertainReviewRecordProposalIds(retainedUncertainProposalIds);
    setAutoRetryRound(0);
  }, [clientTurnKey, recoveryScope, replaceOperations, uniqueClientTurnIds.length]);

  useEffect(() => {
    const allowed = new Set(uniqueClientTurnIds);
    setPendingRecoveryTurnIds((current) => new Set(
      [...current].filter((clientTurnId) => allowed.has(clientTurnId)),
    ));
    setCancellableAbsentTurnIds((current) => new Set(
      [...current].filter((clientTurnId) => allowed.has(clientTurnId)),
    ));
    setAutoRetryRound(0);
  }, [clientTurnKey, externalUncertainTurnKey]);

  useEffect(() => {
    if (!active) return;
    for (const operation of operations) {
      if (seenOperationIdsRef.current.has(operation.operation_id)) continue;
      seenOperationIdsRef.current.add(operation.operation_id);
      onOperationAppeared?.(
        operation.operation_id,
        operation.operation_type,
        operation.client_turn_id,
      );
    }
  }, [active, onOperationAppeared, operations]);

  useEffect(() => {
    if (!active || document.visibilityState === "hidden") {
      requestEpochRef.current += 1;
      loadAbortRef.current?.abort();
      return;
    }
    if (cancellingRecovery
        || actionOperationId !== null
        || preparingReviewRecordOperationId !== null
        || actionOperationIdRef.current !== null
        || reviewRecordActionAbortRef.current !== null) return;

    const controller = new AbortController();
    const epoch = ++requestEpochRef.current;
    loadAbortRef.current?.abort();
    loadAbortRef.current = controller;
    setLoading(true);

    void (async () => {
      const allowedTurnIds = new Set(clientTurnIdsRef.current);
      // A persisted command may be the only surviving browser evidence after a reload. Treat
      // its stable operation ID as known before deciding that a terminal turn is empty.
      const knownOperationIdsByTurn = indexKnownTrustedImmediateOperations(
        allowedTurnIds,
        operationsRef.current.map((operation) => ({
          operationId: operation.operation_id,
          clientTurnId: operation.client_turn_id,
        })),
        [...actionCommandIdsRef.current].map(([operationId, command]) => ({
          operationId,
          clientTurnId: command.clientTurnId,
        })),
      );
      let nextById = new Map(
        operationsRef.current
          .filter((item) => allowedTurnIds.has(item.client_turn_id)
            || (item.operation_type === "review_record"
              && (pendingClarificationOperationIdsRef.current.has(item.operation_id)
                || pendingConfirmationOperationIdsRef.current.has(item.operation_id)
                || uncertainReviewRecordProposalIdsRef.current.has(item.operation_id))))
          .map((item) => [item.operation_id, item]),
      );
      const errors: string[] = [];
      const uncertainTurns = new Set<string>();
      const recoveryPending = new Set<string>();
      const resolvedRecovery = new Set<string>();
      const terminalEmptyTurns = new Set<string>();
      const cancelledTurns = new Set<string>();
      const cancellableAbsentTurns = new Set<string>();
      const canonicallyLoadedActionIds = new Set<string>();
      const turnCanonicalOperationIds = new Set<string>();
      const terminalActionOperationIds = new Set<string>();
      const discoveredProposalOperations = new Map<string, ProposalOperationDiscovery>();
      const turnIds = [...clientTurnIdsRef.current];
      // Turns load concurrently. Within one turn, read the durable owner state before its
      // operation sets so a terminal owner creates a barrier after every operation commit.
      const loadedByTurn = await Promise.all(turnIds.map((clientTurnId) => (
        loadTrustedImmediateOperationTurn(
          () => getChatTurnStatus(clientTurnId, { signal: controller.signal }),
          OPERATION_FAMILY_ADAPTERS.map((adapter) => () => adapter.loadByTurn(
            clientTurnId,
            { signal: controller.signal },
          )),
        )
      )));
      if (controller.signal.aborted || requestEpochRef.current !== epoch) return;
      // Read global clarification membership after the turn barrier so an older global
      // snapshot cannot overwrite a later processing supplement or dynamic target state.
      const [pendingClarificationsResult, pendingConfirmationsResult] = await Promise.allSettled([
        getPendingReviewRecordClarifications({ signal: controller.signal }),
        getPendingReviewRecordConfirmations({ signal: controller.signal }),
      ]);
      if (controller.signal.aborted || requestEpochRef.current !== epoch) return;
      loadedByTurn.forEach(({ operationLists, turnStatus }, index) => {
        const clientTurnId = turnIds[index];
        const returnedIds = new Set<string>();
        let canonicalLists = true;
        let allReceiptsTerminal = true;
        let terminal = false;
        let validTurnStatus = false;
        let turnState: ChatTurnStatus["state"] | null = null;

        if (turnStatus.status === "fulfilled"
            && isChatTurnStatus(turnStatus.value, clientTurnId)) {
          validTurnStatus = true;
          terminal = turnStatus.value.terminal;
          turnState = turnStatus.value.state;
          for (const reference of turnStatus.value.proposal_operations) {
            const proposal = proposalOperationFromServer(
              reference.surface,
              reference.operation_id,
            );
            if (proposal !== null) {
              discoveredProposalOperations.set(
                `${proposal.surface}:${proposal.operationId}`,
                { operation: proposal, clientTurnId },
              );
            }
          }
        } else {
          errors.push(l("本轮状态暂时无法安全读取", "This turn's status cannot be read safely yet"));
          uncertainTurns.add(clientTurnId);
        }

        operationLists.forEach((operationList, familyIndex) => {
          const adapter = OPERATION_FAMILY_ADAPTERS[familyIndex];
          if (!adapter) {
            errors.push(l("本次请求有一种操作暂时无法核对", "One operation in this request cannot be verified yet"));
            uncertainTurns.add(clientTurnId);
            canonicalLists = false;
            return;
          }
          if (operationList.status === "rejected") {
            errors.push(l(`本次${adapter.label.zhCN}结果暂时无法读取`, `The ${adapter.label.en} result cannot be read yet`));
            uncertainTurns.add(clientTurnId);
            canonicalLists = false;
            return;
          }
          const loaded = operationList.value;
          if (!Array.isArray(loaded)) {
            errors.push(l(`本次${adapter.label.zhCN}返回了无法安全识别的结果`, `The ${adapter.label.en} returned an unrecognized result`));
            uncertainTurns.add(clientTurnId);
            canonicalLists = false;
            return;
          }
          if (adapter.maxReceiptsPerTurn !== undefined
              && loaded.length > adapter.maxReceiptsPerTurn) {
            errors.push(
              l(`本次${adapter.label.zhCN}返回的结果数量超出安全范围`, `The ${adapter.label.en} returned too many results to verify safely`),
            );
            uncertainTurns.add(clientTurnId);
            canonicalLists = false;
            return;
          }
          let invalidCount = 0;
          for (const candidate of loaded as unknown[]) {
            if (!adapter.validate(candidate)
                || candidate.operation_type !== adapter.operationType
                || candidate.client_turn_id !== clientTurnId) {
              invalidCount += 1;
              continue;
            }
            nextById.set(candidate.operation_id, candidate);
            turnCanonicalOperationIds.add(candidate.operation_id);
            if (isTrustedActionOperation(candidate)) {
              canonicallyLoadedActionIds.add(candidate.operation_id);
            }
            returnedIds.add(candidate.operation_id);
            if (candidate.operation_type === "review_record"
                && candidate.state === "processing") {
              allReceiptsTerminal = false;
            }
            if (isTrustedActionOperation(candidate)
                && candidate.state !== "completed"
                && trustedOperationIntegrityIssue(candidate) === null) {
              terminalActionOperationIds.add(candidate.operation_id);
            }
          }
          if (invalidCount > 0) {
            errors.push(
              l(`本次${adapter.label.zhCN}有 ${invalidCount} 条无法安全识别或不匹配的结果`, `${invalidCount} ${adapter.label.en} results are unrecognized or mismatched`),
            );
            uncertainTurns.add(clientTurnId);
            canonicalLists = false;
          }
        });
        const missingKnownCount = countMissingKnownTrustedImmediateOperations(
          knownOperationIdsByTurn.get(clientTurnId) ?? [],
          returnedIds,
        );
        if (missingKnownCount > 0) {
          errors.push(l(`本次请求缺少此前已看到的 ${missingKnownCount} 条操作结果`, `This request is missing ${missingKnownCount} operation results that were previously visible`));
          uncertainTurns.add(clientTurnId);
          canonicalLists = false;
        }

        const disposition = classifyTrustedImmediateOperationTurn({
          validTurnStatus,
          terminal,
          turnState,
          allOperationListsCanonical: canonicalLists,
          allOperationReceiptsTerminal: allReceiptsTerminal,
          combinedReceiptCount: returnedIds.size,
          wasUncertain: externalUncertainTurnIds.has(clientTurnId),
        });
        if (disposition !== "pending") {
          if (disposition === "cancelled_with_receipts") {
            errors.push(
              l("本次请求显示已取消，但又返回了操作结果；页面暂不采信", "This request reports cancellation but also returned operation results, so the page will not trust it yet"),
            );
            uncertainTurns.add(clientTurnId);
            recoveryPending.add(clientTurnId);
          } else if (disposition === "cancelled") {
            cancelledTurns.add(clientTurnId);
          } else if (disposition === "terminal_empty") {
            terminalEmptyTurns.add(clientTurnId);
          } else if (disposition === "resolved_recovery") {
            resolvedRecovery.add(clientTurnId);
          }
        } else {
          // Running or absent means the turn set may still grow; read failure or corruption
          // must also preserve uncertainty.
          uncertainTurns.add(clientTurnId);
          recoveryPending.add(clientTurnId);
          if (turnState === "absent" && canonicalLists && returnedIds.size === 0) {
            cancellableAbsentTurns.add(clientTurnId);
          }
        }
      });

      const settledProposalIdsFromTurn = new Set<string>();
      for (const previousId of pendingConfirmationOperationIdsRef.current) {
        const settled = nextById.get(previousId);
        if (settled?.operation_type !== "review_record"
            || settled.state === "pending_confirmation") continue;
        settledProposalIdsFromTurn.add(previousId);
        onReviewProposalSettled?.(
          previousId,
          settled.review_reference,
          settled.client_turn_id,
          retainReviewTurnAfterSettlement(nextById.values(), settled),
        );
        const notice = reviewRecordProposalOutcomeNotice(settled, locale);
        if (notice !== null) {
          storeReviewProposalOutcomeNotice(previousId, notice);
        }
      }

      let nextPendingConfirmationIds = pendingConfirmationOperationIdsRef.current;
      if (pendingConfirmationsResult.status === "rejected") {
        errors.push(l("待确认复盘方案暂时无法读取", "Pending review proposals cannot be read yet"));
      } else if (!Array.isArray(pendingConfirmationsResult.value)
          || pendingConfirmationsResult.value.length > 100) {
        errors.push(l("待确认复盘返回了无法安全识别或数量超限的方案", "Pending reviews returned unrecognized or excessive proposals"));
      } else {
        const loadedIds = new Set<string>();
        const loadedTargets = new Set<number>();
        const loadedConfirmations: ReviewRecordOperation[] = [];
        let invalidCount = 0;
        for (const candidate of pendingConfirmationsResult.value as unknown[]) {
          if (!isReviewRecordOperation(candidate)
              || candidate.state !== "pending_confirmation"
              || candidate.preview === null
              || reviewRecordIntegrityIssue(candidate) !== null) {
            invalidCount += 1;
            continue;
          }
          // The global recovery endpoint only restores turns owned by this tab; proposals
          // from other history or windows must not be inserted into this conversation.
          if (!allowedTurnIds.has(candidate.client_turn_id)) continue;
          if (loadedIds.has(candidate.operation_id)
              || loadedTargets.has(candidate.target_journal_id)) {
            invalidCount += 1;
            continue;
          }
          loadedIds.add(candidate.operation_id);
          loadedTargets.add(candidate.target_journal_id);
          loadedConfirmations.push(candidate);
        }
        if (invalidCount > 0) {
          errors.push(l(`待确认复盘中有 ${invalidCount} 条无法安全识别、重复或指向同一目标的方案`, `${invalidCount} pending review proposals are unrecognized, duplicated, or target the same record`));
        } else {
          for (const previousId of pendingConfirmationOperationIdsRef.current) {
            if (loadedIds.has(previousId)) continue;
            const settled = nextById.get(previousId)
              ?? operationsRef.current.find((item) => item.operation_id === previousId);
            if (uncertainReviewRecordProposalIdsRef.current.has(previousId)
                && settled?.state === "pending_confirmation") {
              loadedIds.add(previousId);
              continue;
            }
            if (settled?.operation_type === "review_record"
                && !settledProposalIdsFromTurn.has(previousId)) {
              onReviewProposalSettled?.(
                previousId,
                settled.review_reference,
                settled.client_turn_id,
                retainReviewTurnAfterSettlement(nextById.values(), settled),
              );
              const notice = reviewRecordProposalOutcomeNotice(settled, locale)
                ?? (settled.state === "pending_confirmation"
                  ? reviewRecordProposalChangedElsewhereNotice(settled.client_turn_id, locale)
                  : null);
              if (notice !== null) {
                storeReviewProposalOutcomeNotice(previousId, notice);
              }
            }
          }
          for (const [operationId, current] of nextById) {
            if (!loadedIds.has(operationId)
                && current.operation_type === "review_record"
                && current.state === "pending_confirmation"
                && !uncertainReviewRecordProposalIdsRef.current.has(operationId)) {
              nextById.delete(operationId);
            }
          }
          for (const candidate of loadedConfirmations) {
            nextById.set(candidate.operation_id, candidate);
          }
          nextPendingConfirmationIds = loadedIds;
        }
      }

      let nextPendingClarificationIds = pendingClarificationOperationIdsRef.current;
      let pendingClarificationUncertain = false;
      if (pendingClarificationsResult.status === "rejected") {
        pendingClarificationUncertain = true;
        errors.push(l("待补充复盘结果暂时无法读取", "Reviews awaiting clarification cannot be read yet"));
      } else if (!Array.isArray(pendingClarificationsResult.value)
          || pendingClarificationsResult.value.length > 100) {
        pendingClarificationUncertain = true;
        errors.push(l("待补充复盘返回了无法安全识别或数量超限的结果", "Clarification recovery returned unrecognized or excessive results"));
      } else {
        const loadedIds = new Set<string>();
        const loadedClarifications: ReviewRecordOperation[] = [];
        let invalidCount = 0;
        for (const candidate of pendingClarificationsResult.value as unknown[]) {
          if (!isReviewRecordOperation(candidate)
              || candidate.state !== "completed"
              || candidate.outcome !== "needs_clarification"
              || candidate.result?.outcome !== "needs_clarification"
              || reviewRecordIntegrityIssue(candidate) !== null
              || loadedIds.has(candidate.operation_id)) {
            invalidCount += 1;
            continue;
          }
          loadedIds.add(candidate.operation_id);
          loadedClarifications.push(candidate);
        }
        if (invalidCount > 0) {
          pendingClarificationUncertain = true;
          errors.push(l(`待补充复盘中有 ${invalidCount} 条无法安全识别或重复的结果`, `${invalidCount} clarification results are unrecognized or duplicated`));
        } else {
          const reconciled = reconcilePendingClarificationSnapshot({
            currentOperations: nextById,
            previousPendingIds: pendingClarificationOperationIdsRef.current,
            loadedPendingOperations: loadedClarifications as TrustedImmediateOperation[],
            allowedTurnIds,
            turnCanonicalOperationIds,
          });
          nextById = reconciled.operations;
          nextPendingClarificationIds = reconciled.pendingIds;
        }
      }
      if (!mountedRef.current || controller.signal.aborted || requestEpochRef.current !== epoch) return;
      pendingConfirmationOperationIdsRef.current = new Set(nextPendingConfirmationIds);
      pendingClarificationOperationIdsRef.current = new Set(nextPendingClarificationIds);
      setPendingClarificationOperationIds(new Set(nextPendingClarificationIds));
      setPendingClarificationReadUncertain(shouldFreezeRetainedClarifications(
        !pendingClarificationUncertain,
        nextPendingClarificationIds,
      ));
      replaceOperations([...nextById.values()]);
      updateUncertainReviewRecordProposalIds((current) => {
        const retained = new Set(
          [...current].filter((operationId) => (
            nextById.get(operationId)?.state === "pending_confirmation"
          )),
        );
        for (const operationId of reviewRecordProposalRecoveryActionsRef.current.keys()) {
          if (!retained.has(operationId)) {
            reviewRecordProposalRecoveryActionsRef.current.delete(operationId);
            const settled = nextById.get(operationId);
            if (settled?.operation_type === "review_record"
                && settled.state !== "pending_confirmation") {
              onReviewProposalSettled?.(
                operationId,
                settled.review_reference,
                settled.client_turn_id,
                retainReviewTurnAfterSettlement(nextById.values(), settled),
              );
              const notice = reviewRecordProposalOutcomeNotice(settled, locale);
              if (notice !== null) {
                storeReviewProposalOutcomeNotice(operationId, notice);
              }
            }
          }
        }
        return retained;
      });
      updateUncertainActionOperationIds((current) => new Set(
        [...current].filter((operationId) => !terminalActionOperationIds.has(operationId)),
      ));
      setOperationErrors((current) => {
        const next = { ...current };
        for (const operationId of canonicallyLoadedActionIds) {
          if (!uncertainActionOperationIdsRef.current.has(operationId)) delete next[operationId];
        }
        return next;
      });
      setUncertainTurnIds(new Set([...uncertainTurns, ...recoveryPending]));
      setPendingRecoveryTurnIds(recoveryPending);
      setCancellableAbsentTurnIds(cancellableAbsentTurns);
      setListError(errors.join("；"));
      if (discoveredProposalOperations.size > 0) {
        onProposalOperationsDiscovered?.([...discoveredProposalOperations.values()]);
      }
      for (const clientTurnId of resolvedRecovery) onTurnResolved?.(clientTurnId);
      for (const clientTurnId of terminalEmptyTurns) onTerminalEmptyTurn?.(clientTurnId);
      for (const clientTurnId of cancelledTurns) onTurnCancelled?.(clientTurnId);
    })().finally(() => {
      if (!mountedRef.current || requestEpochRef.current !== epoch) return;
      if (loadAbortRef.current === controller) loadAbortRef.current = null;
      setLoading(false);
    });

    return () => {
      controller.abort();
      if (loadAbortRef.current === controller) loadAbortRef.current = null;
    };
  }, [active, actionOperationId, cancellingRecovery, clientTurnKey, externalUncertainTurnIds,
    externalUncertainTurnKey, manualRefreshSignal, onProposalOperationsDiscovered,
    onReviewProposalSettled,
    onTerminalEmptyTurn, onTurnCancelled, onTurnResolved,
    preparingReviewRecordOperationId, refreshSignal, replaceOperations,
    storeReviewProposalOutcomeNotice,
    updateUncertainActionOperationIds, updateUncertainReviewRecordProposalIds,
    visibilitySignal]);

  useEffect(() => {
    if (!active || document.visibilityState === "hidden" || loading || cancellingRecovery
        || actionOperationId !== null || preparingReviewRecordOperationId !== null
        || pendingRecoveryTurnIds.size === 0) return;
    const delay = autoRetryRound < UNCERTAIN_RETRY_DELAYS_MS.length
      ? UNCERTAIN_RETRY_DELAYS_MS[autoRetryRound]
      : UNCERTAIN_HEARTBEAT_MS;
    const timer = window.setTimeout(() => {
      setAutoRetryRound((current) => Math.min(
        current + 1,
        UNCERTAIN_RETRY_DELAYS_MS.length,
      ));
      setManualRefreshSignal((current) => current + 1);
    }, delay);
    return () => window.clearTimeout(timer);
  }, [active, actionOperationId, autoRetryRound, cancellingRecovery, listError, loading,
    pendingRecoveryTurnIds.size, pendingRecoveryTurnKey, preparingReviewRecordOperationId,
    visibilitySignal]);

  const cancelAbsentRecoveries = useCallback(async () => {
    if (!active || document.visibilityState === "hidden" || loading || cancellingRecovery
        || actionOperationIdRef.current !== null
        || reviewRecordActionAbortRef.current !== null
        || cancellableAbsentTurnIds.size === 0) return;
    const clientTurnIdsToCancel = [...cancellableAbsentTurnIds];
    const controller = new AbortController();
    recoveryCancelAbortRef.current?.abort();
    recoveryCancelAbortRef.current = controller;
    setCancellingRecovery(true);
    setRecoveryCancelError("");
    try {
      const results = await Promise.allSettled(clientTurnIdsToCancel.map((clientTurnId) => (
        cancelChatTurnIfAbsent(clientTurnId, { signal: controller.signal })
      )));
      if (!mountedRef.current || controller.signal.aborted) return;
      const failures: string[] = [];
      results.forEach((result, index) => {
        const clientTurnId = clientTurnIdsToCancel[index];
        if (result.status === "rejected") {
          failures.push(l(`第 ${index + 1} 项：停止请求失败`, `Item ${index + 1}: stop request failed`));
        } else if (!isChatTurnStatus(result.value, clientTurnId)) {
          failures.push(l(`第 ${index + 1} 项：停止结果无法安全识别`, `Item ${index + 1}: stop result is not safely recognizable`));
        }
      });
      setRecoveryCancelError(failures.join("；"));
    } finally {
      if (recoveryCancelAbortRef.current === controller) recoveryCancelAbortRef.current = null;
      if (mountedRef.current && !controller.signal.aborted) {
        setCancellingRecovery(false);
        setAutoRetryRound(0);
        // Whether cancellation or an existing owner wins, reload the durable turn and
        // canonical operation lists.
        setManualRefreshSignal((current) => current + 1);
      }
    }
  }, [active, cancellableAbsentTurnIds, cancellingRecovery, loading]);

  const prepareReviewRecordUndoBatch = useCallback(async (
    batchOperations: readonly ReviewRecordOperation[],
  ) => {
    const firstOperation = batchOperations[0];
    if (!active
        || interactionDisabled
        || document.visibilityState === "hidden"
        || loadAbortRef.current !== null
        || actionOperationIdRef.current !== null
        || reviewRecordActionAbortRef.current !== null
        || cancellingRecovery
        || firstOperation === undefined
        || batchOperations.some((operation) => (
          operation.client_turn_id !== firstOperation.client_turn_id
          || !isUndoableReviewRecord(operation)
        ))) return;
    const controller = new AbortController();
    reviewRecordActionAbortRef.current = controller;
    setPreparingReviewRecordOperationId(firstOperation.operation_id);
    setReviewRecordNotices((current) => {
      const next = { ...current };
      for (const operation of batchOperations) delete next[operation.operation_id];
      return next;
    });
    setOperationErrors((current) => {
      const next = { ...current };
      for (const operation of batchOperations) delete next[operation.operation_id];
      return next;
    });
    try {
      const candidates = await Promise.all(batchOperations.map((operation) => (
        getReviewRecordOperation(operation.operation_id, { signal: controller.signal })
      )));
      const canonical = candidates.map((candidate, index) => {
        if (!isUndoableReviewRecord(candidate)
            || candidate.operation_id !== batchOperations[index].operation_id
            || candidate.client_turn_id !== firstOperation.client_turn_id) {
          throw new Error(l("本批复盘已有记录发生变化，不能按旧批次生成撤销预览。", "A review in this batch changed, so undo previews cannot use the stale batch."));
        }
        return candidate;
      });
      replaceOperations(operationsRef.current.map((item) => (
        canonical.find((candidate) => candidate.operation_id === item.operation_id) ?? item
      )));
      const preparedOperations = await Promise.all(canonical.map(async (candidate) => {
        const prepared = await prepareReviewRecordUndoOperation(
          candidate.operation_id,
          { signal: controller.signal },
        ) as unknown;
        if (!isPreparedReviewUndoOperation(
          prepared,
          candidate.target_journal_id,
          candidate.result.target_revision,
        )) {
          throw new Error(l("撤销预览无法安全识别，或与当前目标不匹配", "The undo preview is unrecognized or does not match the current target"));
        }
        return { prepared, candidate };
      }));
      if (!mountedRef.current || controller.signal.aborted) return;
      for (const { prepared, candidate } of preparedOperations) {
        onReviewUndoPrepared?.(prepared.operation_id, candidate.client_turn_id);
      }
      setReviewRecordNotices((current) => ({
        ...current,
        [firstOperation.operation_id]: batchOperations.length === 1
          ? l("撤销预览已生成；请在下方独立的复盘撤销卡片中核对影响并确认。", "Undo preview prepared. Review and confirm it in the separate card below.")
          : l(`本批 ${batchOperations.length} 条撤销预览已生成；核对后可统一撤销。`, `Prepared ${batchOperations.length} undo previews; review them before undoing the batch.`),
      }));
    } catch (reason) {
      if (!mountedRef.current || controller.signal.aborted) return;
      setOperationErrors((current) => ({
        ...current,
        [firstOperation.operation_id]: batchOperations.length === 1
          ? l("生成撤销预览失败，请稍后重试。", "Could not prepare the undo preview. Try again later.")
          : l("整批撤销预览未能完整生成，请稍后重试同一批次。", "Could not prepare every undo preview. Retry the same batch later."),
      }));
    } finally {
      if (reviewRecordActionAbortRef.current === controller) {
        reviewRecordActionAbortRef.current = null;
        if (mountedRef.current) setPreparingReviewRecordOperationId(null);
      }
      if (mountedRef.current && !controller.signal.aborted) {
        // Do not scan global undo proposals after a failed response. A user retry retrieves
        // the same preview by target and revision.
        setManualRefreshSignal((current) => current + 1);
      }
    }
  }, [active, cancellingRecovery, interactionDisabled, onReviewUndoPrepared,
    replaceOperations]);

  const runReviewRecordProposalBatchAction = useCallback(async (
    batchOperations: ReviewRecordOperation[],
    decisions: ReviewRecordProposalBatchDecision[],
    action: "approve" | "reject",
  ) => {
    const clientTurnId = batchOperations[0]?.client_turn_id;
    const operationIds = new Set(batchOperations.map((operation) => operation.operation_id));
    const decisionIds = new Set(decisions.map((decision) => decision.operation_id));
    if (!active
        || interactionDisabled
        || document.visibilityState === "hidden"
        || loadAbortRef.current !== null
        || actionOperationIdRef.current !== null
        || reviewRecordActionAbortRef.current !== null
        || reviewRecordProposalActionRef.current !== null
        || cancellingRecovery
        || clientTurnId === undefined
        || batchOperations.length === 0
        || operationIds.size !== batchOperations.length
        || decisionIds.size !== decisions.length
        || decisions.length !== batchOperations.length
        || decisions.some((decision) => !operationIds.has(decision.operation_id))
        || batchOperations.some((operation) => (
          operation.client_turn_id !== clientTurnId
          || operation.state !== "pending_confirmation"
          || operation.preview === null
          || reviewRecordIntegrityIssue(operation) !== null
        ))) return;

    const recoveryConflict = decisions.some((decision) => {
      const recoveryDecision = reviewRecordProposalRecoveryActionsRef.current.get(
        decision.operation_id,
      );
      return recoveryDecision !== undefined
        && JSON.stringify(recoveryDecision) !== JSON.stringify(decision);
    });
    if (recoveryConflict) {
      setOperationErrors((current) => Object.fromEntries([
        ...Object.entries(current),
        ...batchOperations.map((operation) => [
          operation.operation_id,
          l("上次统一确认结果仍不确定；只能继续核对同一次选择。", "The previous batch decision remains uncertain; only the same choice can be rechecked."),
        ]),
      ]));
      return;
    }
    const controller = new AbortController();
    reviewRecordActionAbortRef.current = controller;
    reviewRecordProposalActionRef.current = clientTurnId;
    setReviewRecordProposalAction({ clientTurnId, action });
    for (const decision of decisions) {
      reviewRecordProposalRecoveryActionsRef.current.set(
        decision.operation_id,
        decision,
      );
    }
    updateUncertainReviewRecordProposalIds((current) => new Set([
      ...current,
      ...operationIds,
    ]));
    setOperationErrors((current) => {
      const next = { ...current };
      for (const operationId of operationIds) delete next[operationId];
      return next;
    });

    let canonical: ReviewRecordOperation[] | null = null;
    let submissionRejection: HttpError | null = null;
    const acceptCandidates = (
      candidates: unknown,
      allowAdditional: boolean,
    ): ReviewRecordOperation[] => {
      if (!Array.isArray(candidates)
          || (!allowAdditional && candidates.length !== operationIds.size)) {
        throw new Error(l("统一确认返回的复盘数量与本批次不匹配", "The batch decision returned the wrong number of reviews"));
      }
      const seen = new Set<string>();
      const accepted: ReviewRecordOperation[] = [];
      for (const candidate of candidates as unknown[]) {
        if (!isReviewRecordOperation(candidate)
            || candidate.client_turn_id !== clientTurnId
            || reviewRecordIntegrityIssue(candidate) !== null) {
          throw new Error(l("统一确认结果无法安全识别，或与当前批次不匹配", "A batch-decision result is unrecognized or does not match this batch"));
        }
        if (!operationIds.has(candidate.operation_id)) {
          if (allowAdditional) continue;
          throw new Error(l("统一确认返回了本批次以外的复盘结果", "The batch decision returned a review outside this batch"));
        }
        if (seen.has(candidate.operation_id)) {
          throw new Error(l("统一确认返回了重复的复盘结果", "The batch decision returned a duplicate review result"));
        }
        seen.add(candidate.operation_id);
        accepted.push(candidate);
      }
      if (accepted.length !== operationIds.size) {
        throw new Error(l("统一确认没有返回本批次的全部复盘结果", "The batch decision did not return every review result"));
      }
      return accepted;
    };

    try {
      try {
        canonical = acceptCandidates(await decideReviewRecordOperationsByClientTurn(
          clientTurnId,
          decisions,
          { signal: controller.signal },
        ), false);
      } catch (reason) {
        if (reason instanceof HttpError) submissionRejection = reason;
      }

      for (let attempt = 0;
        canonical === null || canonical.some(
          (operation) => operation.state === "pending_confirmation",
        );
        attempt += 1) {
        if (attempt >= 4 || controller.signal.aborted) break;
        if (!await waitForRetry([250, 750, 1_500, 3_000][attempt], controller.signal)) break;
        try {
          canonical = acceptCandidates(await getReviewRecordOperationsByClientTurn(
            clientTurnId,
            { signal: controller.signal },
          ), true);
        } catch {}
      }
      if (!mountedRef.current || controller.signal.aborted) return;

      if (canonical?.every((operation) => operation.state === "pending_confirmation")
          && submissionRejection !== null) {
        const rejectionStatus = submissionRejection.status;
        updateUncertainReviewRecordProposalIds((current) => {
          const next = new Set(current);
          for (const operationId of operationIds) next.delete(operationId);
          return next;
        });
        for (const operationId of operationIds) {
          reviewRecordProposalRecoveryActionsRef.current.delete(operationId);
        }
        const message = rejectionStatus === 409
          ? l("本批记录已经变化，这次没有写入。请重新核对后再统一确认。", "Records in this batch changed, so nothing was written. Review them before deciding again.")
          : l("这次统一确认没有完成，整批仍未写入。你可以重新确认或全部放弃。", "The batch decision did not complete and nothing was written. Confirm again or reject all.");
        setOperationErrors((current) => Object.fromEntries([
          ...Object.entries(current),
          ...batchOperations.map((operation) => [operation.operation_id, message]),
        ]));
        return;
      }

      if (canonical === null || canonical.some(
        (operation) => operation.state === "pending_confirmation",
      )) {
        setOperationErrors((current) => Object.fromEntries([
          ...Object.entries(current),
          ...batchOperations.map((operation) => [
            operation.operation_id,
            l("统一确认尚无完整可验证结果；页面将只继续核对同一次选择。", "The batch decision has no complete verifiable result yet; the page will only recheck the same choice."),
          ]),
        ]));
        return;
      }

      const nextOperations = [
        ...operationsRef.current.filter((item) => !operationIds.has(item.operation_id)),
        ...canonical,
      ];
      replaceOperations(nextOperations);
      updateUncertainReviewRecordProposalIds((current) => {
        const next = new Set(current);
        for (const operationId of operationIds) next.delete(operationId);
        return next;
      });
      for (const operationId of operationIds) {
        reviewRecordProposalRecoveryActionsRef.current.delete(operationId);
      }
      const retainTurn = canonical.some((settled) => (
        retainReviewTurnAfterSettlement(nextOperations, settled)
      ));
      for (const settled of canonical) {
        onReviewProposalSettled?.(
          settled.operation_id,
          settled.review_reference,
          settled.client_turn_id,
          retainTurn,
        );
        const notice = reviewRecordProposalOutcomeNotice(settled, locale);
        if (notice !== null) {
          storeReviewProposalOutcomeNotice(settled.operation_id, notice);
        }
      }
      setOperationErrors((current) => {
        const next = { ...current };
        for (const operationId of operationIds) delete next[operationId];
        return next;
      });
    } finally {
      if (reviewRecordActionAbortRef.current === controller) {
        reviewRecordActionAbortRef.current = null;
      }
      if (reviewRecordProposalActionRef.current === clientTurnId) {
        reviewRecordProposalActionRef.current = null;
        if (mountedRef.current) setReviewRecordProposalAction(null);
      }
      if (mountedRef.current && !controller.signal.aborted) {
        setManualRefreshSignal((current) => current + 1);
      }
    }
  }, [active, cancellingRecovery, interactionDisabled, onReviewProposalSettled,
    replaceOperations, storeReviewProposalOutcomeNotice,
    updateUncertainReviewRecordProposalIds]);

  useEffect(() => {
    if (!active || document.visibilityState === "hidden" || loading || cancellingRecovery
        || interactionDisabled || actionOperationId !== null
        || preparingReviewRecordOperationId !== null || reviewRecordProposalAction !== null
        || reviewRecordActionAbortRef.current !== null) return;
    const pending = operations.filter((candidate): candidate is ReviewRecordOperation => (
      candidate.operation_type === "review_record"
      && candidate.state === "pending_confirmation"
    ));
    const batch = groupReviewRecordProposalsByTurn(pending).find((candidate) => (
      candidate.operations.every((operation) => (
        uncertainReviewRecordProposalIds.has(operation.operation_id)
        && reviewRecordProposalRecoveryActionsRef.current.has(operation.operation_id)
      ))
    ));
    if (!batch) return;
    const decisions = batch.operations.map((operation) => (
      reviewRecordProposalRecoveryActionsRef.current.get(operation.operation_id)!
    ));
    const action = decisions.every((decision) => decision.action === "reject")
      ? "reject"
      : "approve";
    const timer = window.setTimeout(() => {
      void runReviewRecordProposalBatchAction(batch.operations, decisions, action);
    }, REVIEW_RECORD_PROPOSAL_RECOVERY_MS);
    return () => window.clearTimeout(timer);
  }, [active, actionOperationId, cancellingRecovery, interactionDisabled, loading, operations,
    preparingReviewRecordOperationId, reviewRecordProposalAction,
    runReviewRecordProposalBatchAction, uncertainReviewRecordProposalIds, visibilitySignal]);

  const runUndo = useCallback(async (operation: TrustedActionOperation) => {
    if (actionOperationIdRef.current !== null || loadAbortRef.current !== null
        || reviewRecordActionAbortRef.current !== null
        || !active || document.visibilityState === "hidden" || cancellingRecovery
        || operation.state !== "completed"
        || (!operation.undo_available
          && !uncertainActionOperationIdsRef.current.has(operation.operation_id))
        || trustedOperationIntegrityIssue(operation) !== null) return;

    const operationId = operation.operation_id;
    const operationType = operation.operation_type;
    const adapter = ACTION_OPERATION_ADAPTERS[operationType];
    const existingCommand = actionCommandIdsRef.current.get(operationId);
    if (existingCommand && existingCommand.operationType !== operationType) {
      resumePersistedActionIdsRef.current.delete(operationId);
      updateUncertainActionOperationIds((current) => {
        const next = new Set(current);
        next.delete(operationId);
        return next;
      });
      setOperationErrors((current) => ({
        ...current,
        [operationId]: l("本地恢复信息与当前结果不一致，已停止自动撤销；请核对当前记录。", "Local recovery data conflicts with the current result, so automatic undo stopped. Verify the current record."),
      }));
      return;
    }
    resumePersistedActionIdsRef.current.delete(operationId);
    const commandId = existingCommand?.commandId ?? createSecureCommandId();
    if (commandId === null) {
      setOperationErrors((current) => ({
        ...current,
        [operationId]: l("浏览器无法创建撤销所需的安全编号，撤销尚未提交。", "The browser could not create the secure undo identifier, so undo was not submitted."),
      }));
      return;
    }
    actionCommandIdsRef.current.set(operationId, {
      operationType,
      commandId,
      clientTurnId: operation.client_turn_id,
    });
    if (!updateUncertainActionOperationIds((current) => new Set(current).add(operationId))) {
      actionCommandIdsRef.current.delete(operationId);
      setOperationErrors((current) => ({
        ...current,
        [operationId]: l("浏览器无法保存撤销请求，撤销尚未提交；请检查隐私模式或存储空间。", "The browser could not save the undo request, so it was not submitted. Check private-browsing restrictions or storage space."),
      }));
      return;
    }
    const controller = new AbortController();
    requestEpochRef.current += 1;
    actionAbortRef.current = controller;
    actionOperationIdRef.current = operationId;
    setActionOperationId(operationId);
    setOperationErrors((current) => {
      const next = { ...current };
      delete next[operationId];
      return next;
    });

    const needsCanonicalRefresh = true;
    const stillCurrent = () => mountedRef.current
      && !controller.signal.aborted
      && actionOperationIdRef.current === operationId;
    const markActionUncertain = (message: string) => {
      if (!stillCurrent()) return;
      updateUncertainActionOperationIds((current) => new Set(current).add(operationId));
      setOperationErrors((current) => ({ ...current, [operationId]: message }));
    };

    try {
      let commandStatus: TrustedImmediateUndoCommandStatus | null = null;
      // command_id is the durable identity of this user action. HTTP responses can race or
      // time out; only a terminal tenant-scoped command receipt proves no late write remains.
      for (let attempt = 0; attempt <= UNDO_RETRY_DELAYS_MS.length; attempt += 1) {
        try {
          await adapter.submitUndo(operationId, commandId, { signal: controller.signal });
        } catch {
          if (!stillCurrent()) return;
        }
        try {
          const candidate = await adapter.loadCommandStatus(
            commandId,
            { signal: controller.signal },
          );
          if (!isTrustedImmediateUndoCommandStatus(
            candidate,
            commandId,
            operationId,
            operationType,
          )) {
            throw new Error(l("撤销结果无法安全识别，或与当前请求不匹配", "The undo result is unrecognized or does not match this request"));
          }
          if (candidate.terminal) {
            commandStatus = candidate;
            break;
          }
        } catch {
          if (!stillCurrent()) return;
        }
        markActionUncertain(
          l(`撤销还没有可确认的结果，正在继续安全核对（${attempt + 1}/${UNDO_RETRY_DELAYS_MS.length + 1}）。`, `Undo has no confirmed result yet; continuing safe verification (${attempt + 1}/${UNDO_RETRY_DELAYS_MS.length + 1}).`),
        );
        if (attempt === UNDO_RETRY_DELAYS_MS.length
            || !await waitForRetry(UNDO_RETRY_DELAYS_MS[attempt], controller.signal)) break;
      }
      if (!stillCurrent()) return;

      if (commandStatus === null) {
        markActionUncertain(
          l("自动核对后仍无法确认撤销结果；页面不会把旧结果当成最终状态。可点击“继续核对撤销”重试同一个动作。", "Undo still cannot be confirmed after automatic checks. The page will not treat stale data as final; use Recheck undo to verify the same action."),
        );
        return;
      }

      let canonical: TrustedImmediateOperation | null = null;
      // Once the command terminates durably, fetch the canonical business snapshot only by
      // its original operation ID. Read failures never retarget the role or drop outbox binding.
      for (let attempt = 0; attempt <= UNDO_RETRY_DELAYS_MS.length; attempt += 1) {
        try {
          const candidate = await adapter.loadCanonical(
            operationId,
            { signal: controller.signal },
          );
          if (!isTrustedImmediateOperation(candidate)
              || !isTrustedActionOperation(candidate)
              || candidate.operation_type !== operationType
              || candidate.operation_id !== operationId
              || candidate.client_turn_id !== operation.client_turn_id) {
            throw new Error(l("当前操作结果无法安全识别，或与原请求不匹配", "The current operation result is unrecognized or does not match the original request"));
          }
          canonical = candidate;
          break;
        } catch {
          if (!stillCurrent()) return;
          markActionUncertain(l("暂时无法确认撤销结果；为避免重复操作，此卡片已暂停，系统会继续核对。", "Undo cannot be confirmed yet. This card is paused to prevent duplication, and verification will continue."));
        }
        if (attempt === UNDO_RETRY_DELAYS_MS.length
            || !await waitForRetry(UNDO_RETRY_DELAYS_MS[attempt], controller.signal)) break;
      }
      if (!stillCurrent()) return;

      if (canonical === null) {
        markActionUncertain(
          l("撤销已有明确结果，但当前记录暂时无法读取；为避免重复操作，此卡片暂不可用。可点击“继续核对撤销”重试同一个动作。", "Undo has a definite result, but the current record cannot be read. This card is disabled to prevent duplication; use Recheck undo for the same action."),
        );
        return;
      }

      if (commandStatus.state === "completed" && canonical.state !== "undone") {
        markActionUncertain(
          l("撤销结果与当前操作状态不一致；页面不会猜测，请继续核对。", "The undo result conflicts with the current operation state. The page will not guess; continue verification."),
        );
        return;
      }

      replaceOperations(operationsRef.current.map((item) => (
        item.operation_id === operationId ? canonical : item
      )));
      updateUncertainActionOperationIds((current) => {
        const next = new Set(current);
        next.delete(operationId);
        return next;
      });
      const integrityIssue = trustedOperationIntegrityIssue(canonical);
      if (integrityIssue) {
        setOperationErrors((current) => ({
          ...current,
          [operationId]: l("操作结果未通过完整性核对，请检查当前记录；页面不会宣称撤销成功。", "The operation result failed integrity checks. Verify the current record; the page will not claim undo succeeded."),
        }));
      } else if (canonical.state === "undone") {
        setOperationErrors((current) => {
          const next = { ...current };
          delete next[operationId];
          return next;
        });
      } else if (canonical.state === "completed") {
        setOperationErrors((current) => ({
          ...current,
          [operationId]: l("当前状态仍为“已修改”，尚未确认撤销完成。", "The state is still Updated, so undo is not confirmed."),
        }));
      } else {
        setOperationErrors((current) => ({
          ...current,
          [operationId]: l("旧结果已经失效；无法仅凭此卡判断当前值，请核对现有记录。", "This result is stale. The card alone cannot determine the current value; verify the existing record."),
        }));
      }
    } finally {
      if (actionAbortRef.current === controller) actionAbortRef.current = null;
      if (actionOperationIdRef.current === operationId) {
        actionOperationIdRef.current = null;
        if (mountedRef.current) setActionOperationId(null);
      }
      if (needsCanonicalRefresh && mountedRef.current && active && !document.hidden) {
        setManualRefreshSignal((current) => current + 1);
      }
    }
  }, [active, cancellingRecovery, replaceOperations, updateUncertainActionOperationIds]);

  useEffect(() => {
    if (!active || document.visibilityState === "hidden" || loading
        || actionOperationId !== null || preparingReviewRecordOperationId !== null
        || loadAbortRef.current !== null) return;
    const persisted = operations.find((operation): operation is TrustedActionOperation => (
      isTrustedActionOperation(operation)
      && resumePersistedActionIdsRef.current.has(operation.operation_id)
      && uncertainActionOperationIdsRef.current.has(operation.operation_id)
      && operation.state === "completed"
      && trustedOperationIntegrityIssue(operation) === null
    ));
    if (persisted) void runUndo(persisted);
  }, [active, actionOperationId, loading, operations, preparingReviewRecordOperationId,
    runUndo, visibilitySignal]);

  const pendingReviewProposals = operations.filter(
    (operation): operation is ReviewRecordOperation => (
      operation.operation_type === "review_record"
      && operation.state === "pending_confirmation"
      && operation.preview !== null
      && reviewRecordIntegrityIssue(operation) === null
    ),
  );
  const pendingReviewProposalBatches = groupReviewRecordProposalsByTurn(
    pendingReviewProposals,
  );
  const receiptOperations = operations.filter((operation) => (
    operation.operation_type !== "review_record"
    || operation.state !== "pending_confirmation"
  ));
  const receiptNeedsAttention = (operation: TrustedImmediateOperation): boolean => (
    uncertainTurnIds.has(operation.client_turn_id)
    || uncertainActionOperationIds.has(operation.operation_id)
    || Boolean(operationErrors[operation.operation_id])
    || trustedOperationIntegrityIssue(operation) !== null
    || operation.state === "processing"
    || operation.state === "failed"
    || (operation.operation_type === "review_record"
      && pendingClarificationReadUncertain
      && pendingClarificationOperationIds.has(operation.operation_id))
  );
  const visibleReceiptCandidates = receiptOperations.filter((operation) => (
    visibleClientTurnIdSet.has(operation.client_turn_id)
    || receiptNeedsAttention(operation)
  ));
  const settledReviewReceiptBatches = groupReviewRecordProposalsByTurn(
    visibleReceiptCandidates.filter(isBatchableSettledReviewRecord),
  ).filter((batch) => batch.operations.length > 1);
  const reviewReceiptBatchByOperationId = new Map(settledReviewReceiptBatches.flatMap(
    (batch) => batch.operations.map((operation) => [operation.operation_id, batch.operations]),
  ));
  const visibleReceiptOperations = visibleReceiptCandidates.filter((operation) => {
    const batch = reviewReceiptBatchByOperationId.get(operation.operation_id);
    return batch === undefined || batch[0].operation_id === operation.operation_id;
  });
  const operationIds = new Set(operations.map((operation) => operation.operation_id));
  const orphanProposalNotices = Object.entries(reviewProposalOutcomeNotices).filter(
    ([operationId]) => !operationIds.has(operationId),
  );
  const orphanProposalNoticeGroups = [...orphanProposalNotices.reduce((groups, entry) => {
    const [operationId, notice] = entry;
    const key = `${notice.clientTurnId}\u0000${notice.tone}\u0000${notice.message}`;
    const group = groups.get(key);
    if (group === undefined) groups.set(key, { ...notice, operationIds: [operationId] });
    else group.operationIds.push(operationId);
    return groups;
  }, new Map<string, ReviewRecordProposalNoticeGroup>()).values()];
  const receiptAnchorForTurn = (clientTurnId: string): HTMLElement | null => {
    if (receiptAnchorIdForTurn === undefined || typeof document === "undefined") return null;
    return document.getElementById(receiptAnchorIdForTurn(clientTurnId));
  };
  const anchoredTurnIds = [
    ...pendingReviewProposals.map((operation) => operation.client_turn_id),
    ...visibleReceiptOperations.map((operation) => operation.client_turn_id),
    ...orphanProposalNoticeGroups.map((notice) => notice.clientTurnId),
  ];
  const allTurnContentAnchored = !listError && pendingRecoveryTurnIds.size === 0
    && anchoredTurnIds.length > 0
    && anchoredTurnIds.every((clientTurnId) => receiptAnchorForTurn(clientTurnId) !== null);
  if (!listError && pendingReviewProposals.length === 0
      && visibleReceiptOperations.length === 0
      && pendingRecoveryTurnIds.size === 0
      && orphanProposalNotices.length === 0) return null;

  return (
    <div
      role="region"
      aria-label={l("本轮待确认操作", "Operations awaiting confirmation in this turn")}
      aria-busy={loading || cancellingRecovery || actionOperationId !== null
        || preparingReviewRecordOperationId !== null || reviewRecordProposalAction !== null}
      className={allTurnContentAnchored
        ? "contents"
        : ["flex w-full flex-col gap-2.5", className].filter(Boolean).join(" ")}
    >
      {listError && (
        <div role="alert" className="flex flex-wrap items-center justify-between gap-2 rounded-xl bg-bad-soft px-3 py-2 text-sm text-bad">
          <span>{listError}{l("。本轮已有结果会继续保留。", ". Existing results from this turn remain available.")}</span>
          <button
            type="button"
            onClick={() => {
              setAutoRetryRound(0);
              setManualRefreshSignal((current) => current + 1);
            }}
            disabled={loading || actionOperationId !== null
              || preparingReviewRecordOperationId !== null}
            className="btn btn-sm shrink-0"
          >
            {l("重新核对", "Recheck")}
          </button>
        </div>
      )}
      {pendingRecoveryTurnIds.size > 0 && !listError && (
        <div role="status" className="flex flex-wrap items-center justify-between gap-2 rounded-xl bg-panel-2 px-3 py-2 text-sm text-ink-2">
          <span>
            {autoRetryRound < UNCERTAIN_RETRY_DELAYS_MS.length
              ? l("当前请求正在安全收尾；页面会自动核对本轮结果。", "This request is completing safely; the page will verify its results automatically.")
              : l("本轮仍未确认结束；已有结果会保留，页面已转为每分钟低频核对，也可立即核对。", "This turn is not confirmed complete. Existing results remain available; the page now checks once per minute, or you can check now.")}
            {recoveryCancelError && (
              <span role="alert" className="mt-1 block">
                {l("安全停止失败，恢复记录仍保留：", "Safe stop failed; recovery data remains: ")}{recoveryCancelError}
              </span>
            )}
          </span>
          <span className="flex shrink-0 flex-wrap gap-2">
            <button
              type="button"
              onClick={() => {
                setAutoRetryRound(0);
                setManualRefreshSignal((current) => current + 1);
              }}
              disabled={loading || cancellingRecovery || actionOperationId !== null
                || preparingReviewRecordOperationId !== null}
              className="btn btn-sm shrink-0"
            >
              {l("立即核对", "Check now")}
            </button>
            {autoRetryRound >= UNCERTAIN_RETRY_DELAYS_MS.length
                && cancellableAbsentTurnIds.size > 0 && (
              <button
                type="button"
                onClick={() => void cancelAbsentRecoveries()}
                disabled={loading || cancellingRecovery || actionOperationId !== null
                  || preparingReviewRecordOperationId !== null}
                className="btn btn-sm shrink-0"
                title={l("仅当本轮尚未开始处理时安全停止；若已开始则继续核对", "Stop safely only if processing has not started; otherwise continue verification")}
              >
                {cancellingRecovery ? l("正在安全停止…", "Stopping safely…") : l("安全停止等待", "Stop waiting safely")}
              </button>
            )}
          </span>
        </div>
      )}
      {orphanProposalNoticeGroups.map((notice) => {
        const noticeKey = notice.operationIds.join("-");
        const noticeMessage = notice.operationIds.length > 1
          && notice.message === l("已按确认内容更新求职进展和复盘记录。", "Updated the application and review records as confirmed.")
          ? l(`已按确认内容更新 ${notice.operationIds.length} 条求职进展和复盘记录。`, `Updated ${notice.operationIds.length} application and review records as confirmed.`)
          : notice.message;
        const receipt = (
          <div
            key={`review-proposal-outcome-${noticeKey}`}
            role="status"
            data-client-turn-id={notice.clientTurnId}
            className={[
              "flex flex-wrap items-center justify-between gap-2 rounded-xl border px-3 py-2 text-sm",
              notice.tone === "uncertain"
                ? "border-info/25 bg-info-soft text-info"
                : "border-ok/25 bg-ok-soft text-ok",
            ].join(" ")}
          >
            <span>{noticeMessage}</span>
          </div>
        );
        const anchor = receiptAnchorForTurn(notice.clientTurnId);
        return anchor === null
          ? receipt
          : createPortal(receipt, anchor, `review-proposal-outcome-${noticeKey}`);
      })}
      <div aria-live="polite" className="sr-only">
        {actionOperationId
          ? l("正在核对并撤销已确认的修改", "Checking and undoing the confirmed update")
          : preparingReviewRecordOperationId
            ? l("正在生成复盘撤销预览", "Preparing review undo previews")
            : [...pendingReviewProposals, ...visibleReceiptOperations].map((operation) => trustedOperationAnnouncement(
                operation,
                uncertainActionOperationIds.has(operation.operation_id),
                locale,
              )).join(l("；", "; "))}
      </div>
      {pendingReviewProposalBatches.map((batch) => {
        // The global pending-confirmation endpoint is a discovery/membership index.
        // A failure there must not invalidate an already loaded, integrity-checked
        // proposal snapshot: the batch decision revalidates every operation server-side.
        // Only uncertainty in the proposal's owning turn makes this batch unsafe.
        const readUncertain = uncertainTurnIds.has(batch.clientTurnId);
        const recoveryPending = batch.operations.some((operation) => (
          uncertainReviewRecordProposalIds.has(operation.operation_id)
        ));
        const actionsDisabled = loading || cancellingRecovery || interactionDisabled
          || actionOperationId !== null || preparingReviewRecordOperationId !== null
          || reviewRecordProposalAction !== null || readUncertain;
        const proposal = (
          <ReviewRecordProposalBatchCard
            key={`pending-review-proposal-batch-${batch.clientTurnId}`}
            operations={batch.operations}
            actionsDisabled={actionsDisabled}
            readUncertain={readUncertain}
            action={reviewRecordProposalAction?.clientTurnId === batch.clientTurnId
              ? reviewRecordProposalAction.action
              : null}
            recoveryPending={recoveryPending}
            errors={operationErrors}
            onDecide={(decisions) => void runReviewRecordProposalBatchAction(
              batch.operations,
              decisions,
              decisions.every((decision) => decision.action === "reject")
                ? "reject"
                : "approve",
            )}
          />
        );
        const anchor = receiptAnchorForTurn(batch.clientTurnId);
        return anchor === null
          ? proposal
          : createPortal(
              proposal,
              anchor,
              `pending-review-proposal-batch-${batch.clientTurnId}`,
            );
      })}
      {visibleReceiptOperations.length > 0 && (
        <div className="flex flex-col gap-1.5">
          {visibleReceiptOperations.map((operation) => {
              const reviewBatch = reviewReceiptBatchByOperationId.get(
                operation.operation_id,
              ) ?? null;
              const readUncertain = uncertainTurnIds.has(operation.client_turn_id)
                || (operation.operation_type === "review_record"
                  && pendingClarificationReadUncertain
                  && pendingClarificationOperationIds.has(operation.operation_id));
              const actionsDisabled = loading || cancellingRecovery || actionOperationId !== null
                || preparingReviewRecordOperationId !== null
                || interactionDisabled
                || readUncertain;
              const actionUncertain = uncertainActionOperationIds.has(operation.operation_id);
              const error = operationErrors[operation.operation_id] ?? null;
              const integrityIssue = trustedOperationIntegrityIssue(operation);
              const needsAttention = receiptNeedsAttention(operation);
              const proposalNotice = reviewProposalOutcomeNotices[operation.operation_id];
              const summary = reviewBatch === null
                ? proposalNotice?.message ?? trustedOperationAnnouncement(
                    operation,
                    actionUncertain,
                    locale,
                  )
                : reviewRecordBatchSummary(reviewBatch, locale);
              const canUndo = isTrustedActionOperation(operation)
                && ((operation.state === "completed" && operation.undo_available
                  && integrityIssue === null) || actionUncertain);
              const canPrepareReviewUndo = reviewBatch === null
                && operation.operation_type === "review_record"
                && operation.state === "completed"
                && operation.outcome === "applied"
                && operation.undo_available
                && integrityIssue === null;
              const batchAppliedOperations = reviewBatch?.filter((item) => (
                item.state === "completed" && item.outcome === "applied"
              )) ?? [];
              const canPrepareReviewBatchUndo = batchAppliedOperations.length > 0
                && batchAppliedOperations.every((item) => (
                  item.result?.outcome === "applied"
                  && item.undo_available
                  && reviewRecordIntegrityIssue(item) === null
                ));
              const canSupplement = operation.operation_type === "review_record"
                && operation.state === "completed"
                && operation.outcome === "needs_clarification"
                && pendingClarificationOperationIds.has(operation.operation_id)
                && integrityIssue === null;
              const notice = operation.operation_type === "review_record"
                ? reviewRecordNotices[operation.operation_id] ?? null
                : null;
              const receipt = (
                <div
                  key={reviewBatch === null
                    ? operation.operation_id
                    : `review-batch-${operation.client_turn_id}`}
                  role={needsAttention ? "alert" : "status"}
                  data-client-turn-id={operation.client_turn_id}
                  className={[
                    "flex flex-wrap items-center gap-x-3 gap-y-1.5 rounded-xl border px-3 py-2 text-sm",
                    needsAttention
                      ? "border-warn/25 bg-warn-soft text-warn"
                      : "border-line bg-panel-2/55 text-ink-2",
                  ].join(" ")}
                >
                  <span
                    className={`h-1.5 w-1.5 shrink-0 rounded-full ${
                      needsAttention ? "bg-warn" : "bg-ok"
                    }`}
                    aria-hidden
                  />
                  <span className="min-w-0 flex-1 break-words">
                    {summary}
                    {error && <span className="ml-1 text-bad">{error}</span>}
                    {!error && integrityIssue !== null && (
                      <span className="ml-1 text-bad">{l("结果未通过完整性核对。", "The result failed integrity checks.")}</span>
                    )}
                    {notice && <span className="ml-1 text-ok">{notice}</span>}
                  </span>
                  <span className="flex shrink-0 flex-wrap gap-1.5">
                    {canUndo && (
                      <button
                        type="button"
                        className="btn btn-sm"
                        disabled={actionsDisabled}
                        onClick={() => void runUndo(operation)}
                      >
                        {actionOperationId === operation.operation_id
                          ? l("正在核对…", "Checking…")
                          : actionUncertain ? l("继续核对撤销", "Recheck undo") : l("撤销", "Undo")}
                      </button>
                    )}
                    {canPrepareReviewUndo && (
                      <button
                        type="button"
                        className="btn btn-sm"
                        disabled={actionsDisabled}
                        onClick={() => void prepareReviewRecordUndoBatch([operation])}
                      >
                        {preparingReviewRecordOperationId === operation.operation_id
                          ? l("正在准备…", "Preparing…") : l("撤销", "Undo")}
                      </button>
                    )}
                    {reviewBatch !== null && canPrepareReviewBatchUndo && (
                      <button
                        type="button"
                        className="btn btn-sm"
                        disabled={actionsDisabled}
                        onClick={() => void prepareReviewRecordUndoBatch(batchAppliedOperations)}
                      >
                        {preparingReviewRecordOperationId !== null
                          && batchAppliedOperations.some((item) => (
                            item.operation_id === preparingReviewRecordOperationId
                          ))
                          ? l("正在准备整批撤销…", "Preparing batch undo…") : l("撤销本批", "Undo batch")}
                      </button>
                    )}
                    {canSupplement && (
                      <button
                        type="button"
                        className="btn btn-sm"
                        disabled={actionsDisabled}
                        onClick={() => onReviewClarificationRequested?.(
                          operation.review_reference,
                        )}
                      >
                        {l("补充信息（可选）", "Add details (optional)")}
                      </button>
                    )}
                    {needsAttention && !canUndo && (
                      <button
                        type="button"
                        className="btn btn-sm"
                        disabled={loading || cancellingRecovery}
                        onClick={() => {
                          setAutoRetryRound(0);
                          setManualRefreshSignal((current) => current + 1);
                        }}
                      >
                        {l("重新核对", "Recheck")}
                      </button>
                    )}
                  </span>
                </div>
              );
              const anchor = receiptAnchorForTurn(operation.client_turn_id);
              return anchor === null
                ? receipt
                : createPortal(
                    receipt,
                    anchor,
                    reviewBatch === null
                      ? operation.operation_id
                      : `review-batch-${operation.client_turn_id}`,
                  );
            })}
        </div>
      )}
    </div>
  );
}
