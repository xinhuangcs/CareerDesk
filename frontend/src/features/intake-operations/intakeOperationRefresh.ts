import type { IntakeOperation } from "./intakeOperationContract";

export function mergePendingIntakeOperations(
  current: IntakeOperation[],
  loaded: IntakeOperation[],
  protectedOperationIds: ReadonlySet<string>,
): IntakeOperation[] {
  const loadedIds = new Set(loaded.map((operation) => operation.operation_id));
  const retained = current.filter((operation) => (
    protectedOperationIds.has(operation.operation_id)
      && !loadedIds.has(operation.operation_id)
  ));
  return retained.length === 0 ? loaded : [...loaded, ...retained];
}

export function reconcileIntakeExcludedRows(
  current: Record<string, number[]>,
  loaded: IntakeOperation[],
  protectedOperationIds: ReadonlySet<string>,
): Record<string, number[]> {
  const next: Record<string, number[]> = {};
  for (const operation of loaded) {
    const existing = current[operation.operation_id];
    next[operation.operation_id] = existing === undefined
      ? []
      : existing.filter((rowNumber) => (
          rowNumber >= 1 && rowNumber <= operation.positions.length
        ));
  }
  for (const operationId of protectedOperationIds) {
    if (!(operationId in next) && Object.hasOwn(current, operationId)) {
      next[operationId] = current[operationId];
    }
  }
  return next;
}

export function retainIntakeOperationErrors(
  current: Record<string, string>,
  loaded: IntakeOperation[],
  protectedOperationIds: ReadonlySet<string>,
): Record<string, string> {
  const visibleIds = new Set(loaded.map((operation) => operation.operation_id));
  for (const operationId of protectedOperationIds) visibleIds.add(operationId);
  return Object.fromEntries(
    Object.entries(current).filter(([operationId]) => visibleIds.has(operationId)),
  );
}
