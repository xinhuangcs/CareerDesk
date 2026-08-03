import { lazy, Suspense, type ComponentType } from "react";
import { useLocalizer } from "../../i18n/useLocalizer";

import { ApplicationDeleteOperationsPanel } from "../application-delete-operations/ApplicationDeleteOperationsPanel";
import { ApplicationMergeOperationsPanel } from "../application-merge-operations/ApplicationMergeOperationsPanel";
import { IntakeOperationsPanel } from "../intake-operations/IntakeOperationsPanel";
import type { OperationAnchorIdResolver } from "../operations/operationAnchorPortal";
import type { ProposalOperation } from "./chatProposalRecovery";

export type ProposalPanelProps = {
  active: boolean;
  refreshSignal: number;
  operationIds: readonly string[];
  anchorIdForOperation?: OperationAnchorIdResolver;
  className?: string;
  onOperationAppeared?: (operationId: string) => void;
  onOperationSettled?: (operationId: string) => void;
};

const LazyReviewUndoOperationsPanel = lazy(async () => {
  const module = await import("../review-operations/ReviewUndoOperationsPanel");
  return { default: module.ReviewUndoOperationsPanel };
});

function ReviewUndoOperationsPanel(props: ProposalPanelProps) {
  const l = useLocalizer();
  return (
    <Suspense fallback={<p role="status" className="text-xs text-ink-3">{l("正在加载复盘撤销…", "Loading review undo…")}</p>}>
      <LazyReviewUndoOperationsPanel {...props} />
    </Suspense>
  );
}

/**
 * Confirmation panel for each proposal surface. The Record is exhaustive, so
 * adding an unregistered surface fails compilation without source-text guards.
 */
export const PROPOSAL_PANELS: Record<
  ProposalOperation["surface"],
  ComponentType<ProposalPanelProps>
> = {
  intake: IntakeOperationsPanel,
  application_merge: ApplicationMergeOperationsPanel,
  application_delete: ApplicationDeleteOperationsPanel,
  review_undo: ReviewUndoOperationsPanel,
};

/** Render in registry order: import proposals before destructive proposals. */
export const PROPOSAL_PANEL_SURFACES = Object.keys(PROPOSAL_PANELS) as (
  ProposalOperation["surface"]
)[];
