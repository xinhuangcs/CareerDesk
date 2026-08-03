export type TrustedImmediateOperationTurnLoad<Status, OperationList> = {
  turnStatus: PromiseSettledResult<Status>;
  operationLists: PromiseSettledResult<OperationList>[];
};

/**
 * Durable owner state must be observed before any operation list is read. Once that
 * barrier has passed, all lists are intentionally fetched together so one operation
 * family cannot independently declare a terminal-empty turn.
 */
export async function loadTrustedImmediateOperationTurn<Status, OperationList>(
  loadTurnStatus: () => Promise<Status>,
  loadOperationLists: readonly (() => Promise<OperationList>)[],
): Promise<TrustedImmediateOperationTurnLoad<Status, OperationList>> {
  const [turnStatus] = await Promise.allSettled([loadTurnStatus()]);
  const operationLists = await Promise.allSettled(
    loadOperationLists.map((loadOperationList) => loadOperationList()),
  );
  return { turnStatus, operationLists };
}

export type TrustedImmediateOperationTurnDisposition =
  | "pending"
  | "cancelled"
  | "cancelled_with_receipts"
  | "terminal_empty"
  | "resolved_recovery"
  | "terminal_with_receipts";

export type TrustedImmediateOperationIdentity = {
  operationId: string;
  clientTurnId: string;
};

export function indexKnownTrustedImmediateOperations(
  allowedTurnIds: ReadonlySet<string>,
  loadedOperations: Iterable<TrustedImmediateOperationIdentity>,
  persistedActions: Iterable<TrustedImmediateOperationIdentity>,
): Map<string, Set<string>> {
  const indexed = new Map<string, Set<string>>();
  for (const identity of [...loadedOperations, ...persistedActions]) {
    if (!allowedTurnIds.has(identity.clientTurnId)) continue;
    const known = indexed.get(identity.clientTurnId) ?? new Set<string>();
    known.add(identity.operationId);
    indexed.set(identity.clientTurnId, known);
  }
  return indexed;
}

export function countMissingKnownTrustedImmediateOperations(
  knownOperationIds: Iterable<string>,
  returnedOperationIds: ReadonlySet<string>,
): number {
  let missing = 0;
  for (const operationId of knownOperationIds) {
    if (!returnedOperationIds.has(operationId)) missing += 1;
  }
  return missing;
}

export function classifyTrustedImmediateOperationTurn({
  validTurnStatus,
  terminal,
  turnState,
  allOperationListsCanonical,
  allOperationReceiptsTerminal,
  combinedReceiptCount,
  wasUncertain,
}: {
  validTurnStatus: boolean;
  terminal: boolean;
  turnState: string | null;
  allOperationListsCanonical: boolean;
  allOperationReceiptsTerminal: boolean;
  combinedReceiptCount: number;
  wasUncertain: boolean;
}): TrustedImmediateOperationTurnDisposition {
  if (!validTurnStatus || !terminal || !allOperationListsCanonical) return "pending";
  if (turnState === "cancelled") {
    return combinedReceiptCount > 0 ? "cancelled_with_receipts" : "cancelled";
  }
  if (!allOperationReceiptsTerminal) return "pending";
  if (combinedReceiptCount === 0) return "terminal_empty";
  return wasUncertain ? "resolved_recovery" : "terminal_with_receipts";
}
