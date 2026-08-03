import type { ApplicationStage, Board, BoardItem, NextAction } from "./timelineContract";

type StageProjection = {
  stage: ApplicationStage;
  next_action: NextAction | null;
};

export type BoardItemSnapshot = {
  item: BoardItem;
  stage: ApplicationStage;
  index: number;
};

export type ListItemSnapshot = {
  item: BoardItem;
  index: number;
};

export type HistoryConflictDraft = {
  expected_revision: number;
  update_current_state: boolean;
  set_next_action: boolean;
  conflicted: boolean;
};

export type HistoryConflictResolution<T extends HistoryConflictDraft> = {
  draft: T;
  kind: "safe_rebase" | "confirmation_required";
};

export const HISTORY_ENTRY_EDITABLE_FIELDS = [
  "step",
  "occurred_date",
  "outcome",
  "summary",
] as const;

export type HistoryEntryEditableField = typeof HISTORY_ENTRY_EDITABLE_FIELDS[number];

export type HistoryEntryEditableValues = Record<HistoryEntryEditableField, string>;

export type ExistingHistoryDraft = HistoryEntryEditableValues & {
  expected_revision: number;
  expected_fingerprint: string | null;
  conflicted: boolean;
};

export type ExistingHistoryConflict = {
  fields: HistoryEntryEditableField[];
  latest: HistoryEntryEditableValues;
};

export type ExistingHistoryConflictResolution<T extends ExistingHistoryDraft> = {
  draft: T;
  conflict: ExistingHistoryConflict | null;
  kind: "safe_rebase" | "confirmation_required";
};

export function stageEndsApplication(stage: ApplicationStage): boolean {
  return stage === "rejected" || stage === "withdrawn";
}

export function projectStageMove<T extends StageProjection>(
  value: T,
  targetStage: ApplicationStage,
): T {
  return {
    ...value,
    stage: targetStage,
    next_action: stageEndsApplication(targetStage) ? null : value.next_action,
  };
}

export function captureBoardItem(
  columns: Record<ApplicationStage, BoardItem[]>,
  applicationId: number,
): BoardItemSnapshot | null {
  for (const [stage, items] of Object.entries(columns) as [ApplicationStage, BoardItem[]][]) {
    const index = items.findIndex((item) => item.id === applicationId);
    if (index >= 0) return { item: { ...items[index] }, stage, index };
  }
  return null;
}

export function restoreBoardItem(
  columns: Record<ApplicationStage, BoardItem[]>,
  snapshot: BoardItemSnapshot,
): Record<ApplicationStage, BoardItem[]> {
  const restored = Object.fromEntries(Object.entries(columns).map(([stage, items]) => [
    stage,
    items.filter((item) => item.id !== snapshot.item.id),
  ])) as Record<ApplicationStage, BoardItem[]>;
  const destination = [...restored[snapshot.stage]];
  destination.splice(Math.min(snapshot.index, destination.length), 0, { ...snapshot.item });
  restored[snapshot.stage] = destination;
  return restored;
}

export function restoreListItem(
  items: BoardItem[],
  applicationId: number,
  snapshot: ListItemSnapshot | null,
): BoardItem[] {
  const restored = items.filter((item) => item.id !== applicationId);
  if (snapshot === null) return restored;
  restored.splice(Math.min(snapshot.index, restored.length), 0, { ...snapshot.item });
  return restored;
}

export function captureListItem(
  items: BoardItem[],
  applicationId: number,
): ListItemSnapshot | null {
  const index = items.findIndex((item) => item.id === applicationId);
  return index < 0 ? null : { item: { ...items[index] }, index };
}

export function removeApplicationFromBoard(board: Board, applicationId: number): Board {
  let removed = 0;
  const columns = Object.fromEntries(Object.entries(board.columns).map(([stage, items]) => [
    stage,
    items.filter((item) => {
      if (item.id !== applicationId) return true;
      removed += 1;
      return false;
    }),
  ])) as Board["columns"];
  return removed === 0 ? board : {
    ...board,
    columns,
    total: Math.max(0, board.total - removed),
  };
}

export function historyDraftChangesProjection(
  draft: Pick<HistoryConflictDraft, "update_current_state" | "set_next_action">,
): boolean {
  return draft.update_current_state || draft.set_next_action;
}

export function rebaseHistoryDraftAfterConflict<T extends HistoryConflictDraft>(
  draft: T,
  latestRevision: number,
): HistoryConflictResolution<T> {
  const needsConfirmation = historyDraftChangesProjection(draft);
  return {
    kind: needsConfirmation ? "confirmation_required" : "safe_rebase",
    draft: {
      ...draft,
      expected_revision: latestRevision,
      conflicted: needsConfirmation,
    },
  };
}

export function confirmHistoryDraftAgainstLatest<T extends HistoryConflictDraft>(draft: T): T {
  return { ...draft, conflicted: false };
}

/**
 * Three-way merge for editing an existing history entry.
 *
 * The base is what the user originally opened, the draft contains their edits, and latest is the
 * current server snapshot. Non-overlapping changes are merged automatically. If both sides changed
 * the same field to different values, the user's value stays in the draft but an explicit overwrite
 * confirmation is required before retrying with the latest CAS tokens.
 */
export function rebaseExistingHistoryDraftAfterConflict<T extends ExistingHistoryDraft>(
  base: HistoryEntryEditableValues,
  draft: T,
  latest: HistoryEntryEditableValues,
  latestRevision: number,
  latestFingerprint: string,
): ExistingHistoryConflictResolution<T> {
  const conflictingFields: HistoryEntryEditableField[] = [];
  const merged = { ...draft };
  for (const field of HISTORY_ENTRY_EDITABLE_FIELDS) {
    const userChanged = draft[field] !== base[field];
    const serverChanged = latest[field] !== base[field];
    if (userChanged && serverChanged && draft[field] !== latest[field]) {
      conflictingFields.push(field);
    }
    merged[field] = userChanged ? draft[field] : latest[field];
  }
  const needsConfirmation = conflictingFields.length > 0;
  return {
    kind: needsConfirmation ? "confirmation_required" : "safe_rebase",
    draft: {
      ...merged,
      expected_revision: latestRevision,
      expected_fingerprint: latestFingerprint,
      conflicted: needsConfirmation,
    },
    conflict: needsConfirmation ? {
      fields: conflictingFields,
      latest: { ...latest },
    } : null,
  };
}

export function formatNextActionForImpact(nextAction: NextAction): string {
  const schedule = [nextAction.date, nextAction.time].filter(Boolean).join(" ");
  return schedule ? `“${nextAction.step}”（${schedule}）` : `“${nextAction.step}”`;
}

export function codePointLength(value: string): number {
  return Array.from(value).length;
}

export function limitCodePoints(value: string, maximum: number): string {
  const points = Array.from(value);
  return points.length <= maximum ? value : points.slice(0, maximum).join("");
}

export function limitCodePointsWhileEditing(
  previous: string,
  next: string,
  maximum: number,
): string {
  if (codePointLength(previous) > maximum && codePointLength(next) < codePointLength(previous)) {
    return next;
  }
  return limitCodePoints(next, maximum);
}
