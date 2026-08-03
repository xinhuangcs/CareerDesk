export type CollapsibleTurnReservation = {
  maxScrollY: number;
  spacerHeight: number;
};

function nonNegative(value: number): number {
  return Number.isFinite(value) ? Math.max(0, value) : 0;
}

export function stableTurnScrollY(value: number): number {
  return Math.round(nonNegative(value));
}

export function minimumConversationLeadSpacerHeight(
  userViewportTop: number,
  topInset: number,
): number {
  return stableTurnScrollY(topInset - userViewportTop);
}

export function latestTurnTopInset(viewportWidth: number): number {
  return viewportWidth >= 768 ? 60 : 64;
}

export function wheelTurnCollapseDistance(
  deltaY: number,
  deltaMode: number,
  viewportHeight: number,
): number {
  const unit = deltaMode === 1 ? 40 : deltaMode === 2 ? nonNegative(viewportHeight) : 1;
  return deltaY < 0 ? nonNegative(-deltaY * unit) : 0;
}

export function turnSpacerHeightForMaxScroll(
  currentSpacerHeight: number,
  reservedMaxScrollY: number,
  documentMaxScrollY: number,
): number {
  return nonNegative(
    nonNegative(currentSpacerHeight)
      + nonNegative(reservedMaxScrollY)
      - nonNegative(documentMaxScrollY),
  );
}

export function turnSpacerHeightWithinCollapseLimit(
  requiredSpacerHeight: number,
  collapseLimit: number | null,
): number {
  const required = nonNegative(requiredSpacerHeight);
  return collapseLimit === null ? required : Math.min(required, nonNegative(collapseLimit));
}

export function reanchoredTurnMaxScrollY(
  currentMaxScrollY: number,
  previousUserDocumentTop: number,
  previousTopInset: number,
  currentUserDocumentTop: number,
  currentTopInset: number,
): number {
  const previousAnchor = previousUserDocumentTop - previousTopInset;
  const currentAnchor = currentUserDocumentTop - currentTopInset;
  return stableTurnScrollY(currentMaxScrollY + currentAnchor - previousAnchor);
}

export function collapseTurnReservation(
  reservation: CollapsibleTurnReservation,
  previousScrollY: number,
  currentScrollY: number,
): CollapsibleTurnReservation {
  const contentMovedDownBy = Math.max(0, previousScrollY - currentScrollY);
  const collapsedBy = Math.min(reservation.spacerHeight, contentMovedDownBy);
  return {
    maxScrollY: Math.max(0, reservation.maxScrollY - collapsedBy),
    spacerHeight: reservation.spacerHeight - collapsedBy,
  };
}
