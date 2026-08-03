import { useCallback, useEffect, useRef, useState } from "react";

import {
  useOperationRefreshScheduler,
  type OperationRefreshBatch,
} from "./useOperationRefreshScheduler.ts";

export type BinaryTrustedOperationState = "pending" | "completed" | "rejected" | "stale";

export type BinaryTrustedOperation = {
  operation_id: string;
  state: BinaryTrustedOperationState;
};

export type BinaryTrustedOperationCommand = "approve" | "reject";
export type BinaryTrustedOperationAction = BinaryTrustedOperationCommand | "recheck";

export type BinaryTrustedOperationNotice = {
  operationId: string;
  kind: "status" | "alert";
  message: string;
};

export type BinaryTrustedOperationNoticePayload = Omit<
  BinaryTrustedOperationNotice,
  "operationId"
>;

export type BinaryTrustedOperationApi<T extends BinaryTrustedOperation> = {
  listPending: () => Promise<T[]>;
  get: (operationId: string) => Promise<T>;
  approve: (operationId: string) => Promise<T>;
  reject: (operationId: string) => Promise<T>;
};

export type BinaryTrustedOperationMessages<T extends BinaryTrustedOperation> = {
  listError: (reason: unknown) => string;
  terminalNotice: (operation: T) => BinaryTrustedOperationNoticePayload;
  oppositeCommandNotice: (
    operation: T,
    attempted: BinaryTrustedOperationCommand,
  ) => BinaryTrustedOperationNoticePayload;
  pendingAfterResponse: (command: BinaryTrustedOperationCommand) => string;
  pendingAfterRecovery: (command: BinaryTrustedOperationCommand) => string;
  mismatchedOperation: () => string;
  missingFromPending: (reason: unknown) => string;
  unknownAfterSubmit: (submitReason: unknown, readReason: unknown) => string;
  unknownAfterRecheck: (reason: unknown) => string;
};

type UseBinaryTrustedOperationQueueOptions<T extends BinaryTrustedOperation> = {
  active: boolean;
  refreshSignal: number;
  api: BinaryTrustedOperationApi<T>;
  messages: BinaryTrustedOperationMessages<T>;
  /**
   * When provided, recover only these exact server operation IDs via `get`.
   * Omitting the property uses the page-wide pending list.
   */
  operationIds?: readonly string[];
  pollIntervalMs?: number;
  onOperationSettled?: (operation: T) => void;
};

type UseBinaryTrustedOperationQueueResult<T extends BinaryTrustedOperation> = {
  operations: T[];
  action: { operationId: string; action: BinaryTrustedOperationAction } | null;
  batchAction: BinaryTrustedOperationCommand | null;
  operationErrors: Record<string, string>;
  uncertainOperationIds: ReadonlySet<string>;
  listError: string;
  notices: BinaryTrustedOperationNotice[];
  loading: boolean;
  refreshPendingOperations: (reportError?: boolean, showLoading?: boolean) => Promise<void>;
  runAction: (operation: T, command: BinaryTrustedOperationCommand) => Promise<boolean>;
  runBatchAction: (command: BinaryTrustedOperationCommand) => Promise<void>;
  recheckOperation: (operation: T) => Promise<void>;
};

const MAX_NOTICES = 8;
const NOTICE_DURATION_MS = 2000;

function isOppositeTerminal(
  state: BinaryTrustedOperationState,
  command: BinaryTrustedOperationCommand,
): boolean {
  return (state === "completed" && command === "reject")
    || (state === "rejected" && command === "approve");
}

export function notifyTerminalOperationOnce<T extends BinaryTrustedOperation>(
  knownTerminalOperationIds: Set<string>,
  operation: T,
  onOperationSettled?: (operation: T) => void,
): boolean {
  if (operation.state === "pending"
      || knownTerminalOperationIds.has(operation.operation_id)) return false;
  knownTerminalOperationIds.add(operation.operation_id);
  try {
    onOperationSettled?.(operation);
  } catch {
    // A consumer refresh notification must never roll back a canonical terminal tombstone.
  }
  return true;
}

/**
 * Shared client state machine for binary trusted operations (approve / keep).
 * Cards render frozen data; this layer guards terminal reads, uncertainty, stale lists,
 * cross-window opposite commands, and click concurrency. The scheduler serializes refreshes.
 */
export function useBinaryTrustedOperationQueue<T extends BinaryTrustedOperation>({
  active,
  refreshSignal,
  api,
  messages,
  operationIds,
  pollIntervalMs = 5000,
  onOperationSettled,
}: UseBinaryTrustedOperationQueueOptions<T>): UseBinaryTrustedOperationQueueResult<T> {
  const normalizedOperationIds = operationIds === undefined
    ? null
    : [...new Set(operationIds)];
  const operationIdsKey = normalizedOperationIds?.join(",") ?? null;
  const [operations, setOperations] = useState<T[]>([]);
  const [action, setAction] = useState<{
    operationId: string;
    action: BinaryTrustedOperationAction;
  } | null>(null);
  const [batchAction, setBatchAction] = useState<BinaryTrustedOperationCommand | null>(null);
  const [operationErrors, setOperationErrors] = useState<Record<string, string>>({});
  const [uncertainOperationIds, setUncertainOperationIds] = useState<Set<string>>(
    () => new Set(),
  );
  const [listError, setListError] = useState("");
  const [notices, setNotices] = useState<BinaryTrustedOperationNotice[]>([]);
  const [loading, setLoading] = useState(true);

  const apiRef = useRef(api);
  const messagesRef = useRef(messages);
  const onOperationSettledRef = useRef(onOperationSettled);
  const operationIdsRef = useRef<readonly string[] | null>(normalizedOperationIds);
  apiRef.current = api;
  messagesRef.current = messages;
  onOperationSettledRef.current = onOperationSettled;
  operationIdsRef.current = normalizedOperationIds;

  const operationsRef = useRef<T[]>([]);
  const uncertainOperationIdsRef = useRef<Set<string>>(new Set());
  // A terminal operation ID cannot be revived by an older pending response during this mount.
  const terminalOperationIdsRef = useRef<Set<string>>(new Set());
  const refreshEpochRef = useRef(0);
  const actionRef = useRef<string | null>(null);
  const batchActionRef = useRef<BinaryTrustedOperationCommand | null>(null);
  const mountedRef = useRef(true);
  const noticeTimersRef = useRef(new Map<string, number>());

  const updateOperations = useCallback((updater: (current: T[]) => T[]) => {
    const next = updater(operationsRef.current);
    operationsRef.current = next;
    setOperations(next);
  }, []);

  const updateUncertainOperationIds = useCallback(
    (updater: (current: Set<string>) => Set<string>) => {
      const next = updater(uncertainOperationIdsRef.current);
      uncertainOperationIdsRef.current = next;
      setUncertainOperationIds(next);
    },
    [],
  );

  const storeNotice = useCallback((notice: BinaryTrustedOperationNotice) => {
    const previousTimer = noticeTimersRef.current.get(notice.operationId);
    if (previousTimer !== undefined) window.clearTimeout(previousTimer);
    setNotices((current) => {
      const withoutPrevious = current.filter(
        (item) => item.operationId !== notice.operationId,
      );
      return [...withoutPrevious, notice].slice(-MAX_NOTICES);
    });
    const timer = window.setTimeout(() => {
      noticeTimersRef.current.delete(notice.operationId);
      if (!mountedRef.current) return;
      setNotices((current) => current.filter(
        (item) => item.operationId !== notice.operationId,
      ));
    }, NOTICE_DURATION_MS);
    noticeTimersRef.current.set(notice.operationId, timer);
  }, []);

  useEffect(() => () => {
    for (const timer of noticeTimersRef.current.values()) window.clearTimeout(timer);
    noticeTimersRef.current.clear();
  }, []);

  const applyCanonicalOperation = useCallback((
    operation: T,
    override?: BinaryTrustedOperationNoticePayload,
  ): boolean => {
    // Any newer exact-ID fact invalidates an older pending-list response.
    refreshEpochRef.current += 1;
    if (operation.state === "pending") {
      if (terminalOperationIdsRef.current.has(operation.operation_id)) return true;
      updateOperations((current) => {
        const found = current.some((item) => item.operation_id === operation.operation_id);
        return found
          ? current.map((item) => (
              item.operation_id === operation.operation_id ? operation : item
            ))
          : [...current, operation];
      });
      updateUncertainOperationIds((current) => {
        const next = new Set(current);
        next.delete(operation.operation_id);
        return next;
      });
      setOperationErrors((current) => {
        const next = { ...current };
        delete next[operation.operation_id];
        return next;
      });
      return false;
    }

    notifyTerminalOperationOnce(
      terminalOperationIdsRef.current,
      operation,
      onOperationSettledRef.current,
    );
    updateOperations((current) => current.filter(
      (item) => item.operation_id !== operation.operation_id,
    ));
    updateUncertainOperationIds((current) => {
      const next = new Set(current);
      next.delete(operation.operation_id);
      return next;
    });
    setOperationErrors((current) => {
      const next = { ...current };
      delete next[operation.operation_id];
      return next;
    });
    storeNotice({
      operationId: operation.operation_id,
      ...(override ?? messagesRef.current.terminalNotice(operation)),
    });
    return true;
  }, [storeNotice, updateOperations, updateUncertainOperationIds]);

  const runRefreshBatch = useCallback(async (batch: OperationRefreshBatch) => {
    if (batch.showLoading) setLoading(true);
    try {
      type CanonicalResolution = {
        previous: T;
        canonical: T | null;
        error: unknown;
      };
      const exactOperationIds = operationIdsRef.current;
      const exactOperationIdSet = exactOperationIds === null
        ? null
        : new Set(exactOperationIds);
      const exactReadResults = exactOperationIds === null
        ? null
        : await Promise.all(exactOperationIds.map(async (operationId) => {
          try {
            const canonical = await apiRef.current.get(operationId);
            if (canonical.operation_id !== operationId) {
              throw new Error(messagesRef.current.mismatchedOperation());
            }
            return {
              operationId,
              canonical,
              error: null,
            };
          } catch (error) {
            return { operationId, canonical: null, error };
          }
        }));
      const loaded = (exactReadResults === null
        ? await apiRef.current.listPending()
        : exactReadResults.flatMap((result) => result.canonical === null
          ? []
          : [result.canonical]))
        .filter((operation) => operation.state === "pending"
          && !terminalOperationIdsRef.current.has(operation.operation_id));
      const loadedIds = new Set(loaded.map((operation) => operation.operation_id));
      const disappeared = operationsRef.current.filter(
        (operation) => !loadedIds.has(operation.operation_id)
          && (exactOperationIdSet === null
            || exactOperationIdSet.has(operation.operation_id)),
      );

      // A pending list proves only current pending state. If a card disappears, read its
      // exact ID; retain and freeze it when no terminal state can be verified.
      let exactLoadError: unknown = null;
      let resolutions: CanonicalResolution[];
      if (exactReadResults === null) {
        resolutions = await Promise.all(disappeared.map(async (previous) => {
          try {
            return {
              previous,
              canonical: await apiRef.current.get(previous.operation_id),
              error: null,
            };
          } catch (error) {
            return { previous, canonical: null, error };
          }
        }));
      } else {
        const previousById = new Map(operationsRef.current.map((operation) => [
          operation.operation_id,
          operation,
        ]));
        resolutions = [];
        for (const result of exactReadResults) {
          if (result.canonical !== null && result.canonical.state !== "pending") {
            resolutions.push({
              previous: previousById.get(result.operationId) ?? result.canonical,
              canonical: result.canonical,
              error: null,
            });
          } else if (result.canonical === null) {
            const previous = previousById.get(result.operationId);
            if (previous) {
              resolutions.push({ previous, canonical: null, error: result.error });
            } else {
              exactLoadError ??= result.error;
            }
          }
        }
      }
      if (!batch.isCurrent()) return;

      const nextById = new Map(loaded.map((operation) => [
        operation.operation_id,
        operation,
      ]));
      const unresolved = new Map<string, string>();
      const canonicallyConfirmedPendingIds = new Set<string>();
      const resolvedNotices: BinaryTrustedOperationNotice[] = [];
      for (const resolution of resolutions) {
        if (resolution.canonical === null) {
          if (terminalOperationIdsRef.current.has(
            resolution.previous.operation_id,
          )) continue;
          nextById.set(resolution.previous.operation_id, resolution.previous);
          unresolved.set(
            resolution.previous.operation_id,
            messagesRef.current.missingFromPending(resolution.error),
          );
        } else if (resolution.canonical.state === "pending") {
          if (terminalOperationIdsRef.current.has(
            resolution.canonical.operation_id,
          )) continue;
          nextById.set(resolution.canonical.operation_id, resolution.canonical);
          canonicallyConfirmedPendingIds.add(resolution.canonical.operation_id);
        } else {
          const newlySettled = notifyTerminalOperationOnce(
            terminalOperationIdsRef.current,
            resolution.canonical,
            onOperationSettledRef.current,
          );
          if (newlySettled) {
            resolvedNotices.push({
              operationId: resolution.canonical.operation_id,
              ...messagesRef.current.terminalNotice(resolution.canonical),
            });
          }
        }
      }

      const nextOperations = [...nextById.values()];
      const nextOperationIds = new Set(
        nextOperations.map((operation) => operation.operation_id),
      );
      operationsRef.current = nextOperations;
      setOperations(nextOperations);
      updateUncertainOperationIds((current) => {
        // A pending list cannot clear an uncertain submission. Only an exact-ID read or
        // terminal card cleanup can do so.
        const next = new Set(
          [...current].filter((operationId) => nextOperationIds.has(operationId)),
        );
        for (const operationId of canonicallyConfirmedPendingIds) next.delete(operationId);
        for (const operationId of unresolved.keys()) next.add(operationId);
        return next;
      });
      setOperationErrors((current) => {
        const next: Record<string, string> = {};
        for (const operation of nextOperations) {
          const uncertainMessage = unresolved.get(operation.operation_id);
          if (uncertainMessage) next[operation.operation_id] = uncertainMessage;
          else if (canonicallyConfirmedPendingIds.has(operation.operation_id)) {
            // An exact-ID read proved this operation is pending, so the stale error is cleared.
          } else if (current[operation.operation_id]) {
            next[operation.operation_id] = current[operation.operation_id];
          }
        }
        return next;
      });
      if (resolvedNotices.length > 0) {
        for (const resolved of resolvedNotices) storeNotice(resolved);
      }
      setListError(exactLoadError === null
        ? ""
        : messagesRef.current.listError(exactLoadError));
    } catch (reason) {
      if (!batch.isCurrent()) return;
      if (batch.reportError) setListError(messagesRef.current.listError(reason));
    } finally {
      if (batch.showLoading && mountedRef.current) setLoading(false);
    }
  }, [storeNotice, updateUncertainOperationIds]);

  const refreshPendingOperations = useOperationRefreshScheduler({
    active,
    refreshSignal,
    operationIdsKey,
    pollIntervalMs,
    signalRefreshReportsError: true,
    refreshEpochRef,
    mountedRef,
    runBatch: runRefreshBatch,
  });

  const runAction = useCallback(async (
    operation: T,
    command: BinaryTrustedOperationCommand,
    fromBatch = false,
  ): Promise<boolean> => {
    if (actionRef.current !== null || operation.state !== "pending"
        || (!fromBatch && batchActionRef.current !== null)
        || uncertainOperationIdsRef.current.has(operation.operation_id)) return false;
    actionRef.current = operation.operation_id;
    setAction({ operationId: operation.operation_id, action: command });
    setNotices((current) => current.filter(
      (notice) => notice.operationId !== operation.operation_id,
    ));
    setOperationErrors((current) => {
      const next = { ...current };
      delete next[operation.operation_id];
      return next;
    });

    let finalStateUnknown = false;
    let settled = false;
    try {
      const canonical = command === "approve"
        ? await apiRef.current.approve(operation.operation_id)
        : await apiRef.current.reject(operation.operation_id);
      if (!mountedRef.current) return false;
      const override = isOppositeTerminal(canonical.state, command)
        ? messagesRef.current.oppositeCommandNotice(canonical, command)
        : undefined;
      settled = applyCanonicalOperation(canonical, override);
      if (!settled) {
        setOperationErrors((current) => ({
          ...current,
          [operation.operation_id]: messagesRef.current.pendingAfterResponse(command),
        }));
      }
    } catch (submitReason) {
      if (!mountedRef.current) return false;
      // Recover a lost POST response or concurrent 409 by exact operation ID only.
      try {
        const canonical = await apiRef.current.get(operation.operation_id);
        if (!mountedRef.current) return false;
        const override = isOppositeTerminal(canonical.state, command)
          ? messagesRef.current.oppositeCommandNotice(canonical, command)
          : undefined;
        settled = applyCanonicalOperation(canonical, override);
        if (!settled) {
          setOperationErrors((current) => ({
            ...current,
            [operation.operation_id]: messagesRef.current.pendingAfterRecovery(command),
          }));
        }
      } catch (readReason) {
        if (!mountedRef.current) return false;
        // A late failed recovery read cannot overwrite a terminal result from concurrent polling.
        if (terminalOperationIdsRef.current.has(operation.operation_id)) {
          settled = true;
          return true;
        }
        finalStateUnknown = true;
        refreshEpochRef.current += 1;
        updateUncertainOperationIds((current) => new Set(current).add(
          operation.operation_id,
        ));
        setOperationErrors((current) => ({
          ...current,
          [operation.operation_id]: messagesRef.current.unknownAfterSubmit(
            submitReason,
            readReason,
          ),
        }));
      }
    } finally {
      if (actionRef.current === operation.operation_id) actionRef.current = null;
      if (mountedRef.current) {
        setAction((current) => (
          current?.operationId === operation.operation_id ? null : current
        ));
        if (!finalStateUnknown) void refreshPendingOperations(false);
      }
    }
    return settled;
  }, [applyCanonicalOperation, refreshPendingOperations, updateUncertainOperationIds]);

  const runBatchAction = useCallback(async (
    command: BinaryTrustedOperationCommand,
  ): Promise<void> => {
    if (batchActionRef.current !== null || actionRef.current !== null) return;
    const batch = operationsRef.current.filter((operation) => (
      operation.state === "pending"
      && !uncertainOperationIdsRef.current.has(operation.operation_id)
    ));
    if (batch.length === 0) return;
    batchActionRef.current = command;
    setBatchAction(command);
    try {
      for (const operation of batch) {
        // Stop the serial batch as soon as one result cannot be confirmed terminal.
        if (!await runAction(operation, command, true)) break;
      }
    } finally {
      batchActionRef.current = null;
      if (mountedRef.current) setBatchAction(null);
    }
  }, [runAction]);

  const recheckOperation = useCallback(async (operation: T) => {
    if (actionRef.current !== null || operation.state !== "pending") return;
    actionRef.current = operation.operation_id;
    setAction({ operationId: operation.operation_id, action: "recheck" });
    try {
      const canonical = await apiRef.current.get(operation.operation_id);
      if (!mountedRef.current) return;
      applyCanonicalOperation(canonical);
    } catch (reason) {
      if (!mountedRef.current) return;
      if (terminalOperationIdsRef.current.has(operation.operation_id)) return;
      updateUncertainOperationIds((current) => new Set(current).add(
        operation.operation_id,
      ));
      setOperationErrors((current) => ({
        ...current,
        [operation.operation_id]: messagesRef.current.unknownAfterRecheck(reason),
      }));
    } finally {
      if (actionRef.current === operation.operation_id) actionRef.current = null;
      if (mountedRef.current) {
        setAction((current) => (
          current?.operationId === operation.operation_id ? null : current
        ));
      }
    }
  }, [applyCanonicalOperation, updateUncertainOperationIds]);

  return {
    operations,
    action,
    batchAction,
    operationErrors,
    uncertainOperationIds,
    listError,
    notices,
    loading,
    refreshPendingOperations,
    runAction,
    runBatchAction,
    recheckOperation,
  };
}
