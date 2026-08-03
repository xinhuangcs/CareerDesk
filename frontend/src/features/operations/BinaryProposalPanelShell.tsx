import { useEffect, useRef, type ReactNode } from "react";
import { useLocalizer } from "../../i18n/useLocalizer";

import {
  useBinaryTrustedOperationQueue,
  type BinaryTrustedOperation,
  type BinaryTrustedOperationAction,
  type BinaryTrustedOperationCommand,
  type BinaryTrustedOperationApi,
  type BinaryTrustedOperationMessages,
} from "./useBinaryTrustedOperationQueue";
import {
  operationAnchorElement,
  renderOperationAtAnchor,
  type OperationAnchorIdResolver,
} from "./operationAnchorPortal";

export type BinaryProposalCardRenderProps<T extends BinaryTrustedOperation> = {
  operation: T;
  action: BinaryTrustedOperationAction | null;
  actionsDisabled: boolean;
  uncertain: boolean;
  error: string | null;
  onApprove: () => void;
  onReject: () => void;
  onRecheck: () => void;
};

export type BinaryProposalBatchRenderProps = {
  count: number;
  command: BinaryTrustedOperationCommand | null;
  actionsDisabled: boolean;
  onApproveAll: () => void;
  onRejectAll: () => void;
};

type BinaryProposalPanelShellProps<T extends BinaryTrustedOperation> = {
  active: boolean;
  refreshSignal: number;
  className?: string;
  operationIds?: readonly string[];
  anchorIdForOperation?: OperationAnchorIdResolver;
  api: BinaryTrustedOperationApi<T>;
  messages: BinaryTrustedOperationMessages<T>;
  regionLabel: string;
  loadingLabel: string;
  noticeKeyPrefix: string;
  cardKeyPrefix: string;
  /** Announces the pending count when visible in chat; omit to skip the screen-reader announcement. */
  pendingAnnouncement?: (count: number) => string;
  onOperationSettled?: (operation: T) => void;
  onOperationAppeared?: (operationId: string) => void;
  renderBatchControls?: (props: BinaryProposalBatchRenderProps) => ReactNode;
  renderCard: (props: BinaryProposalCardRenderProps<T>) => ReactNode;
};

/**
 * Shared binary-proposal shell for queue wiring, anchored rendering, notices, errors, and loading.
 * Feature panels provide APIs, copy, and frozen card rendering; the shared state machine owns decisions.
 */
export function BinaryProposalPanelShell<T extends BinaryTrustedOperation>({
  active,
  refreshSignal,
  className = "",
  operationIds,
  anchorIdForOperation,
  api,
  messages,
  regionLabel,
  loadingLabel,
  noticeKeyPrefix,
  cardKeyPrefix,
  pendingAnnouncement,
  onOperationSettled,
  onOperationAppeared,
  renderBatchControls,
  renderCard,
}: BinaryProposalPanelShellProps<T>) {
  const l = useLocalizer();
  const queue = useBinaryTrustedOperationQueue<T>({
    active,
    refreshSignal,
    api,
    messages,
    operationIds,
    onOperationSettled,
  });
  const announcedOperationIdsRef = useRef(new Set<string>());

  // Run after the card DOM commits. The parent decides whether the user is near enough
  // to the bottom to scroll, preventing asynchronous proposals from stealing the viewport.
  useEffect(() => {
    if (!active) return;
    let firstNewOperationId: string | null = null;
    for (const operation of queue.operations) {
      if (announcedOperationIdsRef.current.has(operation.operation_id)) continue;
      announcedOperationIdsRef.current.add(operation.operation_id);
      firstNewOperationId ??= operation.operation_id;
    }
    if (firstNewOperationId !== null) onOperationAppeared?.(firstNewOperationId);
  }, [active, onOperationAppeared, queue.operations]);

  if (!queue.loading && !queue.listError
      && queue.notices.length === 0 && queue.operations.length === 0) return null;
  const operationContentIds = [
    ...queue.notices.map((notice) => notice.operationId),
    ...queue.operations.map((operation) => operation.operation_id),
  ];
  const allOperationContentAnchored = !queue.loading && !queue.listError
    && operationContentIds.length > 0
    && operationContentIds.every((operationId) => (
      operationAnchorElement(operationId, anchorIdForOperation) !== null
    ));
  return (
    <div
      role="region"
      aria-label={regionLabel}
      className={allOperationContentAnchored
        ? "contents"
        : ["flex w-full flex-col gap-2.5", className].filter(Boolean).join(" ")}
    >
      {pendingAnnouncement && (
        <span className="sr-only" aria-live="polite">
          {queue.operations.length > 0 ? pendingAnnouncement(queue.operations.length) : ""}
        </span>
      )}
      {queue.loading && queue.operations.length === 0 && !queue.listError && (
        <p role="status" className="text-xs text-ink-3">{loadingLabel}</p>
      )}
      {queue.notices.map((notice) => renderOperationAtAnchor((
        <div
          key={notice.operationId}
          role={notice.kind}
          className={notice.kind === "alert"
            ? "rounded-xl bg-warn-soft px-3 py-2 text-sm text-warn"
            : "rounded-xl bg-ok-soft px-3 py-2 text-sm text-ok"}
        >
          <span>{notice.message}</span>
        </div>
      ), notice.operationId, anchorIdForOperation,
      `${noticeKeyPrefix}${notice.operationId}`))}
      {queue.listError && (
        <div role="alert" className="flex flex-wrap items-center justify-between gap-2 rounded-xl bg-bad-soft px-3 py-2 text-sm text-bad">
          <span>{queue.listError}</span>
          <button
            type="button"
            onClick={() => void queue.refreshPendingOperations(true, true)}
            disabled={queue.loading}
            className="btn btn-sm"
          >
            {l("重新加载", "Reload")}
          </button>
        </div>
      )}
      {operationIds !== undefined && queue.operations.length > 1
        && renderBatchControls
        && renderOperationAtAnchor(renderBatchControls({
          count: queue.operations.length,
          command: queue.batchAction,
          actionsDisabled: queue.action !== null || queue.batchAction !== null
            || queue.uncertainOperationIds.size > 0,
          onApproveAll: () => void queue.runBatchAction("approve"),
          onRejectAll: () => void queue.runBatchAction("reject"),
        }), queue.operations[0].operation_id, anchorIdForOperation,
        `${cardKeyPrefix}batch-${queue.operations[0].operation_id}`)}
      {queue.operations.map((operation) => renderOperationAtAnchor(renderCard({
        operation,
        action: queue.action?.operationId === operation.operation_id
          ? queue.action.action
          : null,
        actionsDisabled: queue.action !== null || queue.batchAction !== null,
        uncertain: queue.uncertainOperationIds.has(operation.operation_id),
        error: queue.operationErrors[operation.operation_id] ?? null,
        onApprove: () => void queue.runAction(operation, "approve"),
        onReject: () => void queue.runAction(operation, "reject"),
        onRecheck: () => void queue.recheckOperation(operation),
      }), operation.operation_id, anchorIdForOperation,
      `${cardKeyPrefix}${operation.operation_id}`))}
    </div>
  );
}
