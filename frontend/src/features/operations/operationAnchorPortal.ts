import type { ReactNode } from "react";
import { createPortal } from "react-dom";

export type OperationAnchorIdResolver = (operationId: string) => string | null;

export function operationAnchorElement(
  operationId: string,
  anchorIdForOperation?: OperationAnchorIdResolver,
): HTMLElement | null {
  if (anchorIdForOperation === undefined || typeof document === "undefined") return null;
  const anchorId = anchorIdForOperation(operationId);
  return anchorId === null ? null : document.getElementById(anchorId);
}

/**
 * Keep an operation card in its owning assistant turn when that exact turn is
 * still visible. Recoveries without a trustworthy turn binding stay in the
 * panel's normal fallback position.
 */
export function renderOperationAtAnchor(
  content: ReactNode,
  operationId: string,
  anchorIdForOperation?: OperationAnchorIdResolver,
  portalKey = operationId,
): ReactNode {
  const anchor = operationAnchorElement(operationId, anchorIdForOperation);
  return anchor === null ? content : createPortal(content, anchor, portalKey);
}
