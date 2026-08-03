import { useCallback, useEffect, useRef, type MutableRefObject } from "react";

export type OperationRefreshBatch = {
  epoch: number;
  reportError: boolean;
  showLoading: boolean;
  /** True while this batch is the newest epoch and the owner is still mounted. */
  isCurrent: () => boolean;
};

type UseOperationRefreshSchedulerOptions = {
  active: boolean;
  refreshSignal: number;
  /** Reload identity for exact-ID consumers; a change triggers a loading refresh. */
  operationIdsKey?: string | null;
  pollIntervalMs?: number;
  /** Whether a conversation refreshSignal tick may surface a list error. */
  signalRefreshReportsError: boolean;
  /** Owned by the consumer so canonical writes can invalidate stale list reads. */
  refreshEpochRef: MutableRefObject<number>;
  /** Owned by the consumer; the scheduler flips it on mount/unmount. */
  mountedRef: MutableRefObject<boolean>;
  runBatch: (batch: OperationRefreshBatch) => Promise<void>;
};

/**
 * Shared refresh scheduler with a serial worker, request counting, epoch guards, initial/
 * signal/exact-ID triggers, and visibility polling. Callers retain all business semantics.
 */
export function useOperationRefreshScheduler({
  active,
  refreshSignal,
  operationIdsKey = null,
  pollIntervalMs = 5000,
  signalRefreshReportsError,
  refreshEpochRef,
  mountedRef,
  runBatch,
}: UseOperationRefreshSchedulerOptions): (
  reportError?: boolean,
  showLoading?: boolean,
) => Promise<void> {
  const requestedRef = useRef(0);
  const completedRef = useRef(0);
  const workerRef = useRef<Promise<void> | null>(null);
  const reportErrorRef = useRef(false);
  const showLoadingRef = useRef(false);
  const activeRef = useRef(active);
  const seenRefreshSignalRef = useRef(refreshSignal);
  const seenOperationIdsKeyRef = useRef<string | null>(operationIdsKey);
  const runBatchRef = useRef(runBatch);
  runBatchRef.current = runBatch;

  const refresh = useCallback((reportError = true, showLoading = false): Promise<void> => {
    const requestNumber = ++requestedRef.current;
    reportErrorRef.current ||= reportError;
    showLoadingRef.current ||= showLoading;
    const waitForRequest = async () => {
      while (mountedRef.current && completedRef.current < requestNumber) {
        const currentWorker = workerRef.current;
        if (currentWorker) await currentWorker;
        else await Promise.resolve();
      }
    };
    if (workerRef.current) return waitForRequest();

    let worker: Promise<void>;
    worker = (async () => {
      // Coalesce visibility/focus triggers in one event-loop turn before the first request.
      await Promise.resolve();
      while (mountedRef.current && completedRef.current < requestedRef.current) {
        const completeThrough = requestedRef.current;
        const batchReportError = reportErrorRef.current;
        const batchShowLoading = showLoadingRef.current;
        reportErrorRef.current = false;
        showLoadingRef.current = false;
        const epoch = ++refreshEpochRef.current;
        try {
          await runBatchRef.current({
            epoch,
            reportError: batchReportError,
            showLoading: batchShowLoading,
            isCurrent: () => mountedRef.current && refreshEpochRef.current === epoch,
          });
        } finally {
          completedRef.current = completeThrough;
        }
      }
    })().finally(() => {
      if (workerRef.current !== worker) return;
      workerRef.current = null;
        // A refresh may land between the empty check and finally; wake again after clearing.
      if (mountedRef.current && completedRef.current < requestedRef.current) {
        void refresh(false);
      }
    });
    workerRef.current = worker;
    return waitForRequest();
  // mountedRef and refreshEpochRef are caller-owned stable ref containers.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      refreshEpochRef.current += 1;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    void refresh(true, true);
  }, [refresh]);

  useEffect(() => {
    if (seenRefreshSignalRef.current === refreshSignal) return;
    seenRefreshSignalRef.current = refreshSignal;
    void refresh(signalRefreshReportsError);
  }, [refresh, refreshSignal, signalRefreshReportsError]);

  useEffect(() => {
    if (seenOperationIdsKeyRef.current === operationIdsKey) return;
    seenOperationIdsKeyRef.current = operationIdsKey;
    void refresh(true, true);
  }, [operationIdsKey, refresh]);

  useEffect(() => {
    if (!active) {
      activeRef.current = false;
      return;
    }
    if (!activeRef.current) void refresh(false);
    activeRef.current = true;
    let stopped = false;
    let pollTimer: number | null = null;
    let returnTimer: number | null = null;

    const poll = async () => {
      pollTimer = null;
      if (document.visibilityState === "visible") await refresh(false);
      if (!stopped) {
        pollTimer = window.setTimeout(() => void poll(), pollIntervalMs);
      }
    };
    const refreshOnReturn = () => {
      if (document.visibilityState !== "visible") return;
      if (returnTimer !== null) window.clearTimeout(returnTimer);
      returnTimer = window.setTimeout(() => {
        returnTimer = null;
        void refresh(false);
      }, 100);
    };

    document.addEventListener("visibilitychange", refreshOnReturn);
    window.addEventListener("focus", refreshOnReturn);
    pollTimer = window.setTimeout(() => void poll(), pollIntervalMs);
    return () => {
      stopped = true;
      if (pollTimer !== null) window.clearTimeout(pollTimer);
      if (returnTimer !== null) window.clearTimeout(returnTimer);
      document.removeEventListener("visibilitychange", refreshOnReturn);
      window.removeEventListener("focus", refreshOnReturn);
    };
  }, [active, pollIntervalMs, refresh]);

  return refresh;
}
