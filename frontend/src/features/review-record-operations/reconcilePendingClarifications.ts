export type PendingClarificationIdentity = {
  operation_id: string;
  client_turn_id: string;
};

export function reconcilePendingClarificationSnapshot<
  T extends PendingClarificationIdentity,
>({
  currentOperations,
  previousPendingIds,
  loadedPendingOperations,
  allowedTurnIds,
  turnCanonicalOperationIds,
}: {
  currentOperations: ReadonlyMap<string, T>;
  previousPendingIds: ReadonlySet<string>;
  loadedPendingOperations: readonly T[];
  allowedTurnIds: ReadonlySet<string>;
  turnCanonicalOperationIds: ReadonlySet<string>;
}): { operations: Map<string, T>; pendingIds: Set<string> } {
  const operations = new Map(currentOperations);
  const scopedPendingOperations = loadedPendingOperations.filter(
    (item) => allowedTurnIds.has(item.client_turn_id),
  );
  const pendingIds = new Set(scopedPendingOperations.map((item) => item.operation_id));

  for (const operation of scopedPendingOperations) {
  // The global endpoint supplies membership only for turns owned by this tab. Other-window
  // or historical drafts cannot enter current operations or reappear after a read failure.
    if (!turnCanonicalOperationIds.has(operation.operation_id)) {
      operations.set(operation.operation_id, operation);
    }
  }
  for (const operationId of previousPendingIds) {
    const previous = operations.get(operationId);
    if (!pendingIds.has(operationId)
        && previous !== undefined
        && !allowedTurnIds.has(previous.client_turn_id)) operations.delete(operationId);
  }
  return { operations, pendingIds };
}

export function shouldFreezeRetainedClarifications(
  pendingSnapshotCanonical: boolean,
  retainedPendingIds: ReadonlySet<string>,
): boolean {
  return !pendingSnapshotCanonical && retainedPendingIds.size > 0;
}
