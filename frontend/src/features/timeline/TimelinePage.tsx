import { useCallback, useEffect, useRef, useState, type DragEvent } from "react";
import { useLocation } from "react-router-dom";
import type { UiLocale } from "../../i18n/i18n";
import { currentOutputLocale, useLocale } from "../../i18n/localePreference";
import { useLocalizer } from "../../i18n/useLocalizer";
import { formatNumber } from "../../i18n/formatters";
import { Markdown } from "../../Markdown";
import { HttpError, del, getJson, postJson, putJson } from "../../shared/api/transport";
import { ApplicationDeleteOperationsPanel } from "../application-delete-operations/ApplicationDeleteOperationsPanel";
import { IntakeOperationsPanel } from "../intake-operations/IntakeOperationsPanel";
import { prepareApplicationDeleteOperation } from "../application-delete-operations/applicationDeleteOperationApi";
import { ReviewUndoOperationsPanel } from "../review-operations/ReviewUndoOperationsPanel";
import { prepareTimelineReviewUndoOperation } from "../review-operations/reviewUndoOperationApi";
import {
  ResumeAdaptationPanel,
  type ResumeAdaptationPanelStatus,
} from "../resume-adaptation/ResumeAdaptationPanel";
import { getResumeAdaptation } from "../resume-adaptation/resumeAdaptationApi";
import type { ResumeAdaptationResearchAction } from "../resume-adaptation/resumeAdaptationContract";
import type { ApplicationPriority } from "../applications/applicationContract";
import { ApplicationDetail, Board, TimelineStatistics } from "./timelineContract";
import type {
  ApplicationStage,
  BoardItem,
  NextAction,
  TimelineEntry,
  TimelineOutcome,
} from "./timelineContract";
import { TimelineCreateApplicationDialog } from "./TimelineCreateApplicationDialog";
import { TimelineWorkbookImportDialog } from "./TimelineWorkbookImportDialog";
import {
  captureBoardItem,
  captureListItem,
  codePointLength,
  confirmHistoryDraftAgainstLatest,
  formatNextActionForImpact,
  limitCodePoints,
  limitCodePointsWhileEditing,
  projectStageMove,
  rebaseExistingHistoryDraftAfterConflict,
  rebaseHistoryDraftAfterConflict,
  removeApplicationFromBoard,
  restoreBoardItem,
  restoreListItem,
  stageEndsApplication,
  type ExistingHistoryConflict,
  type HistoryEntryEditableField,
  type HistoryEntryEditableValues,
} from "./timelineInteractionState";
import { mergeConflictingApplicationNote } from "./timelineNoteMerge";
import {
  mergeConflictingApplicationProfile,
  timelineProfileDraftChanged,
  type TimelineProfileDraft,
  type TimelineProfileField,
} from "./timelineProfileMerge";
import {
  COLUMNS,
  OUTCOME_LABELS,
  PRIORITY_META,
  RAIL_STAGES,
  TERMINAL_STAGES,
  STAGE_DOT,
  STAGE_STYLES,
  historyEntryIsMeaningful,
  localizedColumns,
  matchesTimelineQuery,
  outcomeLabel,
  sortListRows,
  sortByPriorityAndCreatedTime,
  stageLabel,
  timelineEntryDateLabel,
  timelineEntryNodeClass,
  timelineEntrySummary,
  timelineEntryTitle,
  todayIso,
  upcomingDayGroups,
  upcomingActionLabel,
} from "./timelineDisplay";


const PREP_POLL_INTERVAL_MS = 3000;
const PREP_LEASE_FALLBACK_MS = 6 * 60 * 1000;
const PREP_POLL_REQUEST_TIMEOUT_MS = 10 * 1000;
const PREP_TRIGGER_TIMEOUT_MS = 15 * 1000;
const TIMELINE_REFRESH_TIMEOUT_MS = 12 * 1000;
// Focus refresh covers cross-window changes. Single-flight polling avoids stacking
// slow reads and starving drag-write requests.
const TIMELINE_AUTO_REFRESH_MS = 10 * 1000;
const STAGE_MOVE_NOTICE_TTL_MS = 2_500;
const SKIP_MANUAL_HISTORY_DELETE_CONFIRMATION_KEY =
  "careerdesk.timeline.skipManualHistoryDeleteConfirmation.v1";

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

type PrepGenerationOptions = {
  forceTakeover?: boolean;
  refreshResearch?: boolean;
};

type PrepTriggerResponse = {
  status: "started" | "reused" | "completed" | "error";
  prep_status?: "ready" | "failed";
  message?: string;
  reused?: boolean;
  refresh_applied?: boolean;
  takeover_applied?: boolean;
  retry_after_seconds?: number | null;
};

type HistoryDraft = {
  expected_revision: number;
  entryId: number | null;
  expected_fingerprint: string | null;
  step: string;
  occurred_date: string;
  outcome: Exclude<TimelineOutcome, null> | "";
  summary: string;
  update_current_state: boolean;
  projected_stage: ApplicationStage;
  set_next_action: boolean;
  next_stage: ApplicationStage;
  next_step: string;
  next_date: string;
  next_time: string;
  next_note: string;
  conflicted: boolean;
  entry_conflict: ExistingHistoryConflict | null;
};

function historyEntryEditableValues(entry: TimelineEntry): HistoryEntryEditableValues {
  return {
    step: entry.step ?? "",
    occurred_date: entry.occurred_date ?? "",
    outcome: entry.outcome ?? "",
    summary: entry.summary ?? "",
  };
}

function historyDraftFromEntry(entry: TimelineEntry, expectedRevision = 0): HistoryDraft {
  return {
    expected_revision: expectedRevision,
    entryId: entry.id,
    expected_fingerprint: entry.snapshot_fingerprint,
    step: entry.step ?? "",
    occurred_date: entry.occurred_date ?? "",
    outcome: entry.outcome ?? "",
    summary: entry.summary ?? "",
    update_current_state: false,
    projected_stage: entry.to_stage,
    set_next_action: false,
    next_stage: entry.to_stage,
    next_step: "",
    next_date: "",
    next_time: "",
    next_note: "",
    conflicted: false,
    entry_conflict: null,
  };
}

function historyDraftsFromEntries(
  entries: ApplicationDetail["timeline_entries"],
  expectedRevision = 0,
): Record<number, HistoryDraft> {
  return Object.fromEntries(entries.map(
    (entry) => [entry.id, historyDraftFromEntry(entry, expectedRevision)],
  ));
}

function historyDraftChanged(
  draft: HistoryDraft | undefined,
  entry: TimelineEntry,
): boolean {
  if (!draft) return false;
  const original = historyDraftFromEntry(entry);
  return draft.step !== original.step
    || draft.occurred_date !== original.occurred_date
    || draft.outcome !== original.outcome
    || draft.summary !== original.summary;
}

function rebaseHistoryEditDraftsAgainstLatest(
  baseEntries: TimelineEntry[],
  drafts: Record<number, HistoryDraft>,
  latestEntries: TimelineEntry[],
  latestRevision: number,
): {
  drafts: Record<number, HistoryDraft>;
  outcomes: Record<number, "safe_rebase" | "confirmation_required">;
} {
  const baseById = new Map(baseEntries.map((entry) => [entry.id, entry]));
  const outcomes: Record<number, "safe_rebase" | "confirmation_required"> = {};
  const rebased = Object.fromEntries(latestEntries.map((latestEntry) => {
    const baseEntry = baseById.get(latestEntry.id);
    const draft = drafts[latestEntry.id];
    if (!baseEntry || !draft) {
      return [latestEntry.id, historyDraftFromEntry(latestEntry, latestRevision)];
    }
    const resolution = rebaseExistingHistoryDraftAfterConflict(
      historyEntryEditableValues(baseEntry),
      draft,
      historyEntryEditableValues(latestEntry),
      latestRevision,
      latestEntry.snapshot_fingerprint,
    );
    outcomes[latestEntry.id] = resolution.kind;
    return [latestEntry.id, {
      ...resolution.draft,
      entry_conflict: resolution.conflict,
    }];
  }));
  return { drafts: rebased, outcomes };
}

type ProfileDraft = TimelineProfileDraft;

type NextActionFields = {
  stage: ApplicationStage;
  step: string;
  date: string;
  time: string;
  note: string;
};

type NextActionDraft = NextActionFields & {
  expected_revision: number;
  base_next_action: NextAction | null;
  conflicted: boolean;
};

type CompleteNextActionDraft = {
  expected_revision: number;
  expected_next_action: NextAction;
  conflicted: boolean;
  occurred_date: string;
  outcome: Exclude<TimelineOutcome, null> | "";
  summary: string;
  set_next_action: boolean;
  next_action: NextActionFields;
};

function cloneNextAction(nextAction: NextAction | null): NextAction | null {
  return nextAction === null ? null : { ...nextAction };
}

function sameNextAction(left: NextAction | null, right: NextAction | null): boolean {
  return left?.stage === right?.stage
    && left?.step === right?.step
    && left?.date === right?.date
    && left?.time === right?.time
    && left?.note === right?.note;
}

function nextActionDraftFromDetail(detail: ApplicationDetail): NextActionDraft {
  return {
    expected_revision: detail.revision,
    base_next_action: cloneNextAction(detail.next_action),
    conflicted: false,
    stage: detail.next_action?.stage ?? detail.stage,
    step: detail.next_action?.step ?? "",
    date: detail.next_action?.date ?? "",
    time: detail.next_action?.time ?? "",
    note: detail.next_action?.note ?? "",
  };
}

function nextActionFromDraft(draft: NextActionDraft): NextAction | null {
  if (!draft.step.trim()) return null;
  return {
    stage: draft.stage,
    step: draft.step.trim(),
    date: draft.date || null,
    time: draft.time || null,
    note: draft.note.trim() || null,
  };
}

const PROFILE_FIELD_LABELS_ZH: Record<TimelineProfileField, string> = {
  company: "公司名称",
  position: "岗位名称",
  department: "部门",
  channel: "渠道",
  stage: "当前阶段",
  current_step: "当前环节",
  applied_date: "投递日期",
  pause_reason: "泡池原因",
  jd_text: "岗位描述",
};
const PROFILE_FIELD_LABELS_EN: Record<TimelineProfileField, string> = {
  company: "Company",
  position: "Role",
  department: "Department",
  channel: "Channel",
  stage: "Current stage",
  current_step: "Current step",
  applied_date: "Application date",
  pause_reason: "On-hold reason",
  jd_text: "Job description",
};

function profileDraftFromDetail(detail: ApplicationDetail): ProfileDraft {
  return {
    expected_revision: detail.revision,
    company: detail.company,
    position: detail.position,
    department: detail.department ?? "",
    channel: detail.channel ?? "",
    stage: detail.stage,
    current_step: detail.current_step ?? "",
    applied_date: detail.applied_date ?? "",
    pause_reason: detail.pause_reason ?? "",
    jd_text: detail.jd_text ?? "",
  };
}

type PendingStageMove = {
  expectedStage: ApplicationStage;
  targetStage: ApplicationStage;
};

type SettledStageOverride = {
  detail: ApplicationDetail;
};

type StageMoveNotice = {
  kind: "saved" | "error";
  message: string;
};

type MissingDraftRecovery = {
  title: string;
  text: string;
};

function recoveryText(
  title: string,
  fields: Array<[string, string | null | undefined]>,
  locale: UiLocale = "zh-CN",
): MissingDraftRecovery {
  return {
    title,
    text: fields
      .filter(([, value]) => value !== null && value !== undefined && value !== "")
      .map(([label, value]) => `${label}${locale === "en" ? ": " : "："}${value}`)
      .join("\n"),
  };
}

function readSkipManualHistoryDeleteConfirmation(): boolean {
  try {
    return window.localStorage.getItem(SKIP_MANUAL_HISTORY_DELETE_CONFIRMATION_KEY) === "1";
  } catch {
    return false;
  }
}

function persistSkipManualHistoryDeleteConfirmation(): void {
  try {
    window.localStorage.setItem(SKIP_MANUAL_HISTORY_DELETE_CONFIRMATION_KEY, "1");
  } catch {
    // Storage may be disabled. The in-memory preference still applies for this page lifetime.
  }
}

function moveBoardItem(board: Board, applicationId: number, targetStage: ApplicationStage): Board {
  let moved: BoardItem | null = null;
  const columns = Object.fromEntries(Object.entries(board.columns).map(([stage, items]) => [
    stage,
    items.filter((item) => {
      if (item.id !== applicationId) return true;
      moved = { ...item, stage: targetStage };
      return false;
    }),
  ]));
  if (moved === null) return board;
  columns[targetStage] = [moved, ...(columns[targetStage] ?? [])];
  return { ...board, columns: columns as Board["columns"] };
}

function patchBoardItem(
  board: Board,
  applicationId: number,
  patch: Partial<BoardItem>,
): Board {
  let changed = false;
  const columns = Object.fromEntries(Object.entries(board.columns).map(([stage, items]) => [
    stage,
    items.map((item) => {
      if (item.id !== applicationId) return item;
      changed = true;
      return { ...item, ...patch };
    }),
  ]));
  return changed ? { ...board, columns: columns as Board["columns"] } : board;
}

function projectPendingStageMoves(
  board: Board,
  pendingMoves: ReadonlyMap<number, PendingStageMove>,
): Board {
  let projected = board;
  pendingMoves.forEach((move, applicationId) => {
    const source = Object.values(projected.columns).flat().find(
      (item) => item.id === applicationId,
    );
    if (!source) return;
    const pendingProjection = projectStageMove(source, move.targetStage);
    projected = patchBoardItem(
      moveBoardItem(projected, applicationId, move.targetStage),
      applicationId,
      {
        stage: pendingProjection.stage,
        next_action: pendingProjection.next_action,
      },
    );
  });
  return projected;
}

function projectSettledStageOverrides(
  board: Board,
  overrides: Map<number, SettledStageOverride>,
): Board {
  let projected = board;
  overrides.forEach((override, applicationId) => {
    const authoritative = override.detail;
    const serverItem = Object.values(projected.columns).flat().find(
      (item) => item.id === applicationId,
    );
    if (!serverItem) return;
    if (serverItem.revision >= authoritative.revision) {
      overrides.delete(applicationId);
      return;
    }
    projected = patchBoardItem(
      moveBoardItem(projected, applicationId, authoritative.stage),
      applicationId,
      {
        stage: authoritative.stage,
        current_step: authoritative.current_step,
        next_action: authoritative.next_action,
        revision: authoritative.revision,
        applied_date: authoritative.applied_date,
        pause_reason: authoritative.pause_reason,
        paused_from_stage: authoritative.paused_from_stage,
      },
    );
  });
  return projected;
}

function projectLoadedApplicationDetail(
  loaded: ApplicationDetail,
  pendingMoves: ReadonlyMap<number, PendingStageMove>,
  settledOverrides: Map<number, SettledStageOverride>,
): ApplicationDetail {
  const settled = settledOverrides.get(loaded.id);
  let projected = loaded;
  if (settled) {
    if (loaded.revision < settled.detail.revision) projected = settled.detail;
    else settledOverrides.delete(loaded.id);
  }
  const pending = pendingMoves.get(loaded.id);
  return pending ? projectStageMove(projected, pending.targetStage) : projected;
}

export function TimelinePage() {
  const l = useLocalizer();
  const { locale } = useLocale();
  const columns = localizedColumns(locale);
  const prepMeta: Record<string, [string, string]> = {
    pending: [l("调研排队中", "Research queued"), "bg-ink-3"],
    running: [l("调研生成中", "Research in progress"), "bg-info"],
    ready: [l("调研就绪", "Research ready"), "bg-ok"],
    failed: [l("调研生成失败", "Research failed"), "bg-bad"],
  };
  const location = useLocation();
  const [board, setBoard] = useState<Board | null>(null);
  const [upcoming, setUpcoming] = useState<BoardItem[]>([]);
  const [upcomingCollapsed, setUpcomingCollapsed] = useState(false);
  const [detail, setDetail] = useState<ApplicationDetail | null>(null);
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [briefing, setBriefing] = useState("");
  const [briefingLoading, setBriefingLoading] = useState(false);   // Covers the gap between selecting Research and its first render.
  const [stageFilter, setStageFilter] = useState<string | null>(null);   // null means all stages in list view.
  const [view, setView] = useState<"board" | "list">("board");   // Board (stages left to right) or searchable, filterable list.
  const [search, setSearch] = useState("");
  const [createApplicationOpen, setCreateApplicationOpen] = useState(false);
  const [workbookImportOpen, setWorkbookImportOpen] = useState(false);
  const [intakeRefreshSignal, setIntakeRefreshSignal] = useState(0);
  const [statisticsOpen, setStatisticsOpen] = useState(false);
  const [statistics, setStatistics] = useState<TimelineStatistics | null>(null);
  const [statisticsLoading, setStatisticsLoading] = useState(false);
  const [statisticsError, setStatisticsError] = useState("");
  const [error, setError] = useState("");
  const [externalChangeNotice, setExternalChangeNotice] = useState("");
  const [missingDraftRecovery, setMissingDraftRecovery] = useState<MissingDraftRecovery | null>(null);
  const [pollMessage, setPollMessage] = useState("");
  const [pollTimedOut, setPollTimedOut] = useState(false);
  const [noteDraft, setNoteDraft] = useState("");
  const [noteDirty, setNoteDirty] = useState(false);
  const [noteEditing, setNoteEditing] = useState(false);
  const [noteExpected, setNoteExpected] = useState<string | null>(null);
  const [noteExpectedRevision, setNoteExpectedRevision] = useState<number | null>(null);
  const [noteConflict, setNoteConflict] = useState<{
    savedNote: string | null;
    rebased: boolean;
  } | null>(null);
  const [noteSaving, setNoteSaving] = useState(false);
  const [detailEditing, setDetailEditing] = useState(false);
  const [profileDraft, setProfileDraft] = useState<ProfileDraft | null>(null);
  const [profileBase, setProfileBase] = useState<ProfileDraft | null>(null);
  const [profileConflict, setProfileConflict] = useState<{
    fields: TimelineProfileField[];
    acknowledged: boolean;
  } | null>(null);
  const [profileDirty, setProfileDirty] = useState(false);
  const [profileSaving, setProfileSaving] = useState(false);
  const [prioritySaving, setPrioritySaving] = useState(false);
  const [currentStepEditing, setCurrentStepEditing] = useState(false);
  const [currentStepDraft, setCurrentStepDraft] = useState("");
  const [currentStepSaving, setCurrentStepSaving] = useState(false);
  const [nextActionDraft, setNextActionDraft] = useState<NextActionDraft | null>(null);
  const [completionDraft, setCompletionDraft] = useState<CompleteNextActionDraft | null>(null);
  const [nextActionSaving, setNextActionSaving] = useState(false);
  const [deletePreparing, setDeletePreparing] = useState(false);
  const [deleteOperationIds, setDeleteOperationIds] = useState<string[]>([]);
  const [deleteRefreshSignal, setDeleteRefreshSignal] = useState(0);
  const [reviewUndoPreparingId, setReviewUndoPreparingId] = useState<number | null>(null);
  const [reviewUndoOperationIds, setReviewUndoOperationIds] = useState<string[]>([]);
  const [reviewUndoRefreshSignal, setReviewUndoRefreshSignal] = useState(0);
  const [historyEditDrafts, setHistoryEditDrafts] = useState<Record<number, HistoryDraft>>({});
  const [historyDraft, setHistoryDraft] = useState<HistoryDraft | null>(null);
  const [historySaving, setHistorySaving] = useState(false);
  const [pendingHistoryDelete, setPendingHistoryDelete] = useState<
    TimelineEntry | null
  >(null);
  const [skipManualHistoryDeleteConfirmation, setSkipManualHistoryDeleteConfirmation] =
    useState(readSkipManualHistoryDeleteConfirmation);
  const [rememberHistoryDeleteChoice, setRememberHistoryDeleteChoice] = useState(false);
  const [stageMovePending, setStageMovePending] = useState<Set<number>>(new Set());
  const [stageMoveNotices, setStageMoveNotices] = useState<Record<number, StageMoveNotice>>({});
  const [detailTab, setDetailTab] = useState<"overview" | "research" | "adaptation">("overview");   // The three drawer tabs.
  const [adaptationStatus, setAdaptationStatus] = useState<ResumeAdaptationPanelStatus>("idle");
  const [stageMenuOpen, setStageMenuOpen] = useState(false);   // The header stage pill changes the stage directly.
  const [priorityMenuOpen, setPriorityMenuOpen] = useState(false);
  const [jdExpanded, setJdExpanded] = useState(false);   // Collapsible job description.
  const historyDeleteCancelRef = useRef<HTMLButtonElement | null>(null);
  const drawerRef = useRef<HTMLElement | null>(null);   // Move focus here on open and trap Tab inside.
  const drawerReturnFocusRef = useRef<HTMLElement | null>(null);   // Return focus to the originating card on close.
  const currentStepInputRef = useRef<HTMLInputElement | null>(null);
  const closeDetailRef = useRef<() => void>(() => {});   // Escape uses the latest close handler, including its unsaved-change guard.
  const pollRef = useRef<number | null>(null);
  const pollEpochRef = useRef(0);
  const selectionEpochRef = useRef(0);   // Detect stale A→B→A responses that an ID comparison alone cannot catch.
  const selectedIdRef = useRef<number | null>(null);   // Lets polling callbacks verify that the selected role is still current.
  const briefingRequestRef = useRef(0);
  const pageMountedRef = useRef(true);
  const boardRefreshEpochRef = useRef(0);
  const refreshErrorRef = useRef<string | null>(null);
  const pendingStageMovesRef = useRef<Map<number, PendingStageMove>>(new Map());
  const settledStageOverridesRef = useRef<Map<number, SettledStageOverride>>(new Map());
  const autoRefreshPromiseRef = useRef<Promise<ApplicationDetail | null> | null>(null);
  const detailRefreshBlockedRef = useRef(false);
  const stageMoveNoticeTimersRef = useRef<Map<number, number>>(new Map());
  const returnRefreshPromiseRef = useRef<Promise<void> | null>(null);
  const returnRefreshQueuedRef = useRef(false);
  const handledDeepLinkRef = useRef("");
  const briefingOpenRef = useRef(false);   // Poll completion reads the latest tab state without a stale closure.
  briefingOpenRef.current = briefing !== "";
  const prepRunning = detail?.prep_status === "pending" || detail?.prep_status === "running";
  const completionTargetStage = completionDraft?.outcome === "failed"
    ? "rejected"
    : detail?.next_action?.stage ?? null;
  const completionClosesApplication = completionTargetStage !== null
    && TERMINAL_STAGES.includes(completionTargetStage);
  const nextActionDraftInvalid = nextActionDraft !== null
    && !stageEndsApplication(profileDraft?.stage ?? detail?.stage ?? nextActionDraft.stage) && (
    Boolean(nextActionDraft.time && !nextActionDraft.date)
    || Boolean(
      !nextActionDraft.step.trim()
      && (nextActionDraft.date || nextActionDraft.time || nextActionDraft.note.trim()),
    )
  );
  const today = todayIso();
  const historyEditsDirty = detail?.timeline_entries.some((entry) => (
    historyDraftChanged(historyEditDrafts[entry.id], entry)
  )) ?? false;
  const detailRefreshBlocked = profileSaving
    || noteSaving
    || historySaving
    || nextActionSaving
    || currentStepEditing
    || currentStepSaving
    || detailEditing
    || noteEditing
    || nextActionDraft !== null
    || completionDraft !== null
    || historyDraft !== null
    || historyEditsDirty
    || Boolean(detail && stageMovePending.has(detail.id));
  detailRefreshBlockedRef.current = detailRefreshBlocked;

  const clearMissingApplication = useCallback((
    id: number,
    recovery: MissingDraftRecovery | null = null,
  ) => {
    const wasSelected = selectedIdRef.current === id;
    if (wasSelected) {
      selectedIdRef.current = null;
      setSelectedId(null);
      selectionEpochRef.current += 1;
      pollEpochRef.current += 1;
      if (pollRef.current !== null) clearTimeout(pollRef.current);
      pollRef.current = null;
      briefingRequestRef.current += 1;
      setDetail(null);
      setAdaptationStatus("idle");
      setDetailLoading(false);
      setNextActionDraft(null);
      setCompletionDraft(null);
      setNextActionSaving(false);
      setPrioritySaving(false);
      setBriefing("");
      setBriefingLoading(false);
      setPollMessage("");
      setPollTimedOut(false);
      setNoteEditing(false);
      setNoteSaving(false);
      setDetailEditing(false);
      setProfileDraft(null);
      setProfileBase(null);
      setProfileConflict(null);
      setProfileDirty(false);
      setProfileSaving(false);
      setCurrentStepEditing(false);
      setCurrentStepDraft("");
      setCurrentStepSaving(false);
      setDeletePreparing(false);
      setDeleteOperationIds([]);
      setReviewUndoPreparingId(null);
      setReviewUndoOperationIds([]);
      setNoteExpected(null);
      setNoteExpectedRevision(null);
      setNoteConflict(null);
      setNoteDirty(false);
      setHistoryEditDrafts({});
      setHistoryDraft(null);
      setHistorySaving(false);
      setPendingHistoryDelete(null);
      setRememberHistoryDeleteChoice(false);
      setError("");
    }
    pendingStageMovesRef.current.delete(id);
    settledStageOverridesRef.current.delete(id);
    setStageMovePending((current) => {
      if (!current.has(id)) return current;
      const next = new Set(current);
      next.delete(id);
      return next;
    });
    const noticeTimer = stageMoveNoticeTimersRef.current.get(id);
    if (noticeTimer !== undefined) window.clearTimeout(noticeTimer);
    stageMoveNoticeTimersRef.current.delete(id);
    setStageMoveNotices((current) => {
      if (!(id in current)) return current;
      const next = { ...current };
      delete next[id];
      return next;
    });
    // A detail 404 may arrive before the next board refresh. Remove the stable ID now
    // so the user cannot dismiss the notice and select a card that no longer exists.
    setBoard((current) => current === null ? current : removeApplicationFromBoard(current, id));
    setUpcoming((current) => current.filter((item) => item.id !== id));
    setMissingDraftRecovery(recovery?.text ? recovery : null);
    setExternalChangeNotice(wasSelected
      ? l("这条岗位已在另一窗口删除或合并；求职进展已刷新，旧详情已关闭。", "This role was deleted or merged in another window. The tracker has refreshed and the old details are closed.")
      : l("这条岗位已在另一窗口删除或合并；对应卡片和近期安排已移除。", "This role was deleted or merged in another window. Its card and upcoming action were removed."));
  }, []);

  const loadDetail = useCallback(async (
    id: number,
    selectionEpoch = selectionEpochRef.current,
    signal?: AbortSignal,
    forceDraftRebase = false,
    missingRecovery: MissingDraftRecovery | null = null,
  ): Promise<ApplicationDetail | null> => {
    try {
      const loaded = ApplicationDetail.parse(
        await getJson<unknown>(`/api/timeline/applications/${id}`, {
          cache: "no-store",
          signal,
        }),
      );
      if (selectedIdRef.current !== id || selectionEpochRef.current !== selectionEpoch) return null;
      const projected = projectLoadedApplicationDetail(
        loaded,
        pendingStageMovesRef.current,
        settledStageOverridesRef.current,
      );
      if (forceDraftRebase || !detailRefreshBlockedRef.current) setDetail(projected);
      return projected;
    } catch (e) {
      if (selectedIdRef.current === id && selectionEpochRef.current === selectionEpoch) {
        if (e instanceof HttpError && e.status === 404) {
          clearMissingApplication(id, missingRecovery);
        }
        else setError(isAbortError(e)
          ? l("求职进展刷新超时，请稍后重试", "Refreshing the tracker timed out. Try again later.")
          : e instanceof Error ? e.message : l("岗位详情加载失败", "Could not load role details"));
      }
      return null;
    }
  }, [clearMissingApplication]);

  async function reconcileMissingTimelineEntry(
    applicationId: number,
    existingApplicationMessage: string,
    recovery: MissingDraftRecovery | null = null,
  ): Promise<ApplicationDetail | null> {
    // A child-resource 404 only proves that the history entry is gone. Re-read the parent before
    // deciding whether to close the job, otherwise a concurrent entry edit/delete looks like the
    // entire application disappeared.
    const loaded = await loadDetail(
      applicationId,
      selectionEpochRef.current,
      undefined,
      true,
      recovery,
    );
    if (!loaded) return null; // loadDetail clears the job only when this parent GET is also 404.
    setError("");
    setExternalChangeNotice(existingApplicationMessage);
    setMissingDraftRecovery(recovery?.text ? recovery : null);
    return loaded;
  }

  const refresh = useCallback(async (selectedId?: number,
                                     selectionEpoch = selectionEpochRef.current): Promise<ApplicationDetail | null> => {
    const refreshEpoch = ++boardRefreshEpochRef.current;
    const controller = new AbortController();
    const requestTimeout = window.setTimeout(
      () => controller.abort(),
      TIMELINE_REFRESH_TIMEOUT_MS,
    );
    const detailPromise = selectedId === undefined || detailRefreshBlockedRef.current
      ? Promise.resolve(null)
      : loadDetail(selectedId, selectionEpoch, controller.signal);
    try {
      const [boardPayload, upcomingPayload] = await Promise.all([
        getJson<unknown>("/api/timeline/board", {
          cache: "no-store",
          signal: controller.signal,
        }),
        getJson<unknown>("/api/timeline/upcoming?days=7", {
          cache: "no-store",
          signal: controller.signal,
        }),
      ]);
      const b = Board.parse(boardPayload);
      const u = Board.parseUpcoming(upcomingPayload);
      if (!pageMountedRef.current || boardRefreshEpochRef.current !== refreshEpoch) {
        return await detailPromise;
      }
      const projectedBoard = projectSettledStageOverrides(
        b,
        settledStageOverridesRef.current,
      );
      setBoard(projectPendingStageMoves(projectedBoard, pendingStageMovesRef.current));
      setUpcoming(u.items.map((item) => {
        const override = settledStageOverridesRef.current.get(item.id);
        let projectedItem = item;
        if (override && item.revision < override.detail.revision) {
          const authoritative = override.detail;
          projectedItem = {
            ...item,
            stage: authoritative.stage,
            current_step: authoritative.current_step,
            next_action: authoritative.next_action,
            revision: authoritative.revision,
            applied_date: authoritative.applied_date,
            pause_reason: authoritative.pause_reason,
            paused_from_stage: authoritative.paused_from_stage,
          };
        }
        const pending = pendingStageMovesRef.current.get(item.id);
        return pending ? projectStageMove(projectedItem, pending.targetStage) : projectedItem;
      }));
      const currentSelectedId = selectedIdRef.current;
      if (currentSelectedId !== null) {
        const stillExists = Object.values(b.columns).some(
          (items) => items.some((item) => item.id === currentSelectedId),
        );
        if (!stillExists) {
          clearMissingApplication(currentSelectedId);
          controller.abort();
        }
      }
      const previousRefreshError = refreshErrorRef.current;
      refreshErrorRef.current = null;
      if (previousRefreshError !== null) {
        setError((current) => current === previousRefreshError ? "" : current);
      }
      return await detailPromise;
    } catch (e) {
      if (pageMountedRef.current && boardRefreshEpochRef.current === refreshEpoch) {
        const message = isAbortError(e)
          ? l("求职进展刷新超时，请稍后重试", "Refreshing the tracker timed out. Try again later.")
          : e instanceof Error ? e.message : l("加载失败", "Loading failed");
        refreshErrorRef.current = message;
        setError(message);
      }
      return await detailPromise;
    } finally {
      window.clearTimeout(requestTimeout);
    }
  }, [clearMissingApplication, loadDetail]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      if (
        document.visibilityState !== "visible"
        || autoRefreshPromiseRef.current !== null
        || pendingStageMovesRef.current.size > 0
      ) return;
      const id = selectedIdRef.current;
      const pending = refresh(id ?? undefined, selectionEpochRef.current);
      autoRefreshPromiseRef.current = pending;
      void pending.finally(() => {
        if (autoRefreshPromiseRef.current === pending) autoRefreshPromiseRef.current = null;
      });
    }, TIMELINE_AUTO_REFRESH_MS);
    return () => window.clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    if (!detail || noteEditing) return;
    setNoteDraft(detail.application_note ?? "");
    setNoteDirty(false);
  }, [detail, noteEditing]);

  useEffect(() => {
    if (!pendingHistoryDelete) return;
    const previousFocus = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    historyDeleteCancelRef.current?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape") return;
      event.preventDefault();
      setPendingHistoryDelete(null);
      setRememberHistoryDeleteChoice(false);
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("keydown", handleKeyDown);
      previousFocus?.focus();
    };
  }, [pendingHistoryDelete?.id]);

  useEffect(() => {
    if (!detail || !pendingHistoryDelete) return;
    if (detail.timeline_entries.some((entry) => entry.id === pendingHistoryDelete.id)) return;
    setPendingHistoryDelete(null);
    setRememberHistoryDeleteChoice(false);
  }, [detail, pendingHistoryDelete]);

  // Escape first yields to history deletion, then closes menus, then closes the guarded drawer.
  useEffect(() => {
    if (selectedId === null) return;
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key !== "Escape" || pendingHistoryDelete) return;
      event.preventDefault();
      if (stageMenuOpen || priorityMenuOpen) {
        setStageMenuOpen(false);
        setPriorityMenuOpen(false);
        return;
      }
      closeDetailRef.current();
    };
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [selectedId, pendingHistoryDelete, priorityMenuOpen, stageMenuOpen]);

  // Enforce the modal focus contract: lock background scrolling, trap Tab, and restore the originating card.
  // Move initial focus only after content is ready to avoid racing the asynchronous detail load.
  useEffect(() => {
    if (selectedId === null) return;
    const drawer = drawerRef.current;
    drawerReturnFocusRef.current = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    const trapTab = (event: KeyboardEvent) => {
      if (event.key !== "Tab" || !drawer) return;
      const items = Array.from(drawer.querySelectorAll<HTMLElement>(
        'button:not([disabled]), [href], input:not([disabled]), textarea:not([disabled]), '
        + 'select:not([disabled]), [tabindex]:not([tabindex="-1"])',
      )).filter((element) => element.offsetParent !== null);
      if (items.length === 0) {
        event.preventDefault();
        drawer.focus();
        return;
      }
      const first = items[0];
      const last = items[items.length - 1];
      const active = document.activeElement;
      if (event.shiftKey && (active === first || active === drawer)) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && (active === last || !drawer.contains(active))) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", trapTab);
    return () => {
      document.body.style.overflow = previousOverflow;
      document.removeEventListener("keydown", trapTab);
      drawerReturnFocusRef.current?.focus();
    };
  }, [selectedId]);

  // Move focus into the ready drawer once, without disturbing focus already inside it.
  useEffect(() => {
    if (selectedId === null || detailLoading) return;
    const drawer = drawerRef.current;
    if (!drawer || drawer.contains(document.activeElement)) return;
    const first = drawer.querySelector<HTMLElement>(
      'button:not([disabled]), [href], input:not([disabled]), textarea:not([disabled]), select:not([disabled])',
    );
    (first ?? drawer).focus();
  }, [selectedId, detailLoading]);

  useEffect(() => {
    if (!currentStepEditing) return;
    currentStepInputRef.current?.focus();
    currentStepInputRef.current?.select();
  }, [currentStepEditing]);

  useEffect(() => {
    if (adaptationStatus !== "running" || detailTab === "adaptation" || selectedId === null) return;
    let cancelled = false;
    let timer: number | null = null;
    let controller: AbortController | null = null;

    const schedule = () => {
      if (cancelled) return;
      timer = window.setTimeout(() => void poll(), PREP_POLL_INTERVAL_MS);
    };
    const poll = async () => {
      controller = new AbortController();
      const timeout = window.setTimeout(
        () => controller?.abort(),
        PREP_POLL_REQUEST_TIMEOUT_MS,
      );
      try {
        const next = await getResumeAdaptation(selectedId, { signal: controller.signal });
        if (cancelled) return;
        if (next.state !== "generation_running" && next.state !== "research_running") {
          setAdaptationStatus(next.state === "ok" ? "ready" : "idle");
          return;
        }
      } catch {
        if (cancelled) return;
      } finally {
        window.clearTimeout(timeout);
      }
      schedule();
    };

    schedule();
    return () => {
      cancelled = true;
      if (timer !== null) window.clearTimeout(timer);
      controller?.abort();
    };
  }, [adaptationStatus, detailTab, selectedId]);

  // Another tab may approve a delete or merge. Coalesce visibility/focus noise on return,
  // then serially reload the board and current detail without allowing recovery requests to overwrite each other.
  useEffect(() => {
    let stopped = false;
    let returnTimer: number | null = null;

    const runReturnRefresh = (): Promise<void> => {
      if (returnRefreshPromiseRef.current) {
        returnRefreshQueuedRef.current = true;
        return returnRefreshPromiseRef.current;
      }
      let worker: Promise<void>;
      worker = (async () => {
        do {
          returnRefreshQueuedRef.current = false;
          const id = selectedIdRef.current;
          const selectionEpoch = selectionEpochRef.current;
          await refresh(id ?? undefined, selectionEpoch);
        } while (!stopped && returnRefreshQueuedRef.current);
      })().finally(() => {
        if (returnRefreshPromiseRef.current === worker) {
          returnRefreshPromiseRef.current = null;
        }
        if (!stopped && returnRefreshQueuedRef.current) void runReturnRefresh();
      });
      returnRefreshPromiseRef.current = worker;
      return worker;
    };

    const scheduleReturnRefresh = () => {
      if (document.visibilityState !== "visible") return;
      if (returnTimer !== null) window.clearTimeout(returnTimer);
      returnTimer = window.setTimeout(() => {
        returnTimer = null;
        void runReturnRefresh();
      }, 100);
    };

    document.addEventListener("visibilitychange", scheduleReturnRefresh);
    window.addEventListener("focus", scheduleReturnRefresh);
    return () => {
      stopped = true;
      returnRefreshQueuedRef.current = false;
      if (returnTimer !== null) window.clearTimeout(returnTimer);
      document.removeEventListener("visibilitychange", scheduleReturnRefresh);
      window.removeEventListener("focus", scheduleReturnRefresh);
    };
  }, [refresh]);

  // Poll serially and use the epoch to isolate responses from prior selections or polling runs.
  // Stop and expose takeover only when the server's precise remaining lease allows it.
  const startPolling = useCallback((id: number, notice = "") => {
    if (pollRef.current !== null) clearTimeout(pollRef.current);
    pollRef.current = null;
    const epoch = ++pollEpochRef.current;
    const selectionEpoch = selectionEpochRef.current;
    const startedAt = Date.now();
    setPollMessage(notice);
    setPollTimedOut(false);
    setNoteDirty(false);

    const stillCurrent = () => pollEpochRef.current === epoch
      && selectionEpochRef.current === selectionEpoch && selectedIdRef.current === id;
    const stop = () => {
      if (pollEpochRef.current === epoch) pollEpochRef.current += 1;
      if (pollRef.current !== null) clearTimeout(pollRef.current);
      pollRef.current = null;
    };
    const allowTakeover = (message: string) => {
      if (!stillCurrent()) return;
      stop();
      setPollTimedOut(true);
      setPollMessage(message);
    };
    const schedule = () => {
      if (!stillCurrent()) return;
      pollRef.current = window.setTimeout(() => void pollOnce(), PREP_POLL_INTERVAL_MS);
    };
    const pollOnce = async () => {
      if (!stillCurrent()) return;
      pollRef.current = null;
      let loaded: ApplicationDetail;
      const controller = new AbortController();
      const requestTimeout = window.setTimeout(
        () => controller.abort(), PREP_POLL_REQUEST_TIMEOUT_MS);
      try {
        loaded = ApplicationDetail.parse(
          await getJson<unknown>(`/api/timeline/applications/${id}`, {
            cache: "no-store",
            signal: controller.signal,
          }),
        );
      } catch {
        if (!stillCurrent()) return;
        // One network failure does not end the background task; keep polling and report a refresh failure.
        if (Date.now() - startedAt >= PREP_LEASE_FALLBACK_MS) {
          allowTakeover(l(
            "已超过安全租约且暂时无法读取任务状态，可以安全接管并重试。",
            "The safe lease expired and the task status is unavailable. You can take over and retry.",
          ));
        } else {
          setPollMessage(l(
            "暂时无法刷新调研进度，正在自动重试…",
            "Research progress is temporarily unavailable. Retrying automatically…",
          ));
          schedule();
        }
        return;
      } finally {
        clearTimeout(requestTimeout);
      }
      if (!stillCurrent()) return;
      // Write details only while this role remains selected.
      if (!detailRefreshBlockedRef.current) {
        setDetail(projectLoadedApplicationDetail(
          loaded,
          pendingStageMovesRef.current,
          settledStageOverridesRef.current,
        ));
      }
      const done = loaded.prep_status !== "pending" && loaded.prep_status !== "running";
      if (done) {
        stop();
        setPollTimedOut(false);
        setPollMessage("");
        if (briefingOpenRef.current) void loadBriefing(id);
        // Polling for a prior role may refresh the board but must never overwrite the newly selected details.
        void refresh();
        return;
      }
      if ((loaded.prep_retry_after_seconds ?? 1) <= 0) {
        allowTakeover(l(
          "后台任务已超过安全租约，可以接管旧任务并重新生成。",
          "The background task exceeded its safe lease. You can take it over and regenerate.",
        ));
        return;
      }
      setPollMessage(notice);
      schedule();
    };
    schedule();
  }, [l, refresh]);

  function hasUnsavedDetailWork(): boolean {
    const historyDirty = detail?.timeline_entries.some((entry) => (
      historyDraftChanged(historyEditDrafts[entry.id], entry)
    )) ?? false;
    return profileSaving
      || noteSaving
      || historySaving
      || nextActionSaving
      || currentStepEditing
      || currentStepSaving
      || nextActionDraft !== null
      || completionDraft !== null
      || (detailEditing && profileDirty)
      || (noteEditing && noteDirty)
      || historyDraft !== null
      || (detailEditing && historyDirty);
  }

  function guardUnsavedDetailExit(): boolean {
    if (!hasUnsavedDetailWork()) return false;
    setError(l(
      "还有未保存的岗位信息、当前环节、备注或历程，请先保存或取消后再收起或切换岗位。",
      "Role details, the current step, notes, or history still have unsaved changes. Save or cancel them before closing or switching roles.",
    ));
    return true;
  }

  async function select(id: number) {
    // Selecting the already-open role needs no reload or scrolling.
    if (selectedIdRef.current === id && (detail !== null || detailLoading)) return;
    if (guardUnsavedDetailExit()) return;
    const selectionEpoch = ++selectionEpochRef.current;
    selectedIdRef.current = id;   // Update synchronously so old requests become stale before React renders.
    setSelectedId(id);
    pollEpochRef.current += 1;
    if (pollRef.current !== null) clearTimeout(pollRef.current);
    pollRef.current = null;
    briefingRequestRef.current += 1;
    setDetail(null);
    setAdaptationStatus("idle");
    setDetailLoading(true);
    setDetailTab("overview");
    setStageMenuOpen(false);
    setPriorityMenuOpen(false);
    setJdExpanded(false);
    setBriefing("");
    setBriefingLoading(false);
    setError("");
    setExternalChangeNotice("");
    setMissingDraftRecovery(null);
    setPollMessage("");
    setPollTimedOut(false);
    setNoteEditing(false);
    setDetailEditing(false);
    setProfileDraft(null);
    setProfileBase(null);
    setProfileConflict(null);
    setProfileDirty(false);
    setProfileSaving(false);
    setCurrentStepEditing(false);
    setCurrentStepDraft("");
    setCurrentStepSaving(false);
    setNextActionDraft(null);
    setCompletionDraft(null);
    setNextActionSaving(false);
    setDeleteOperationIds([]);
    setReviewUndoPreparingId(null);
    setReviewUndoOperationIds([]);
    setNoteExpected(null);
    setNoteExpectedRevision(null);
    setNoteConflict(null);
    setNoteDirty(false);
    setHistoryEditDrafts({});
    setHistoryDraft(null);
    setPendingHistoryDelete(null);
    setRememberHistoryDeleteChoice(false);
    const controller = new AbortController();
    const requestTimeout = window.setTimeout(
      () => controller.abort(),
      TIMELINE_REFRESH_TIMEOUT_MS,
    );
    try {
      const loaded = ApplicationDetail.parse(
        await getJson<unknown>(`/api/timeline/applications/${id}`, {
          cache: "no-store",
          signal: controller.signal,
        }),
      );
      if (selectedIdRef.current !== id || selectionEpochRef.current !== selectionEpoch) return;
      setDetail(projectLoadedApplicationDetail(
        loaded,
        pendingStageMovesRef.current,
        settledStageOverridesRef.current,
      ));
      // Resume polling when an automatic or previously triggered preparation is still running.
      if (loaded.prep_status === "pending" || loaded.prep_status === "running") {
        if ((loaded.prep_retry_after_seconds ?? 1) <= 0) {
          setPollTimedOut(true);
          setPollMessage(l("后台任务已超过安全租约，可以接管旧任务并重新生成。", "The background task exceeded its safe lease. You can take it over and regenerate."));
        } else {
          startPolling(id);
        }
      }
    } catch (e) {
      if (selectedIdRef.current === id && selectionEpochRef.current === selectionEpoch) {
        if (e instanceof HttpError && e.status === 404) clearMissingApplication(id);
        else setError(isAbortError(e)
          ? l("岗位详情加载超时，请稍后重试", "Loading role details timed out. Try again later.")
          : e instanceof Error ? e.message : l("岗位详情加载失败", "Could not load role details"));
      }
    } finally {
      window.clearTimeout(requestTimeout);
      if (selectedIdRef.current === id && selectionEpochRef.current === selectionEpoch) {
        setDetailLoading(false);
      }
    }
  }

  useEffect(() => {
    const params = new URLSearchParams(location.search);
    const applicationValue = params.get("application");
    if (!applicationValue || !/^\d+$/.test(applicationValue)) return;
    const applicationId = Number(applicationValue);
    if (!Number.isSafeInteger(applicationId) || applicationId <= 0) return;
    if (!board || !Object.values(board.columns).some(
      (items) => items.some((item) => item.id === applicationId),
    )) return;
    const key = `${location.key}:${location.search}`;
    if (handledDeepLinkRef.current === key) return;
    handledDeepLinkRef.current = key;
    void select(applicationId).then(() => {
      if (selectedIdRef.current !== applicationId) return;
      const tab = params.get("tab");
      if (tab === "adaptation") setDetailTab("adaptation");
      if (tab === "research") {
        setDetailTab("research");
        void loadBriefing(applicationId);
      }
    });
  }, [board, location.key, location.search]);

  function closeDetail() {
    if (guardUnsavedDetailExit()) return;
    selectedIdRef.current = null;
    setSelectedId(null);
    selectionEpochRef.current += 1;
    pollEpochRef.current += 1;
    if (pollRef.current !== null) clearTimeout(pollRef.current);
    pollRef.current = null;
    briefingRequestRef.current += 1;
    setDetail(null);
    setAdaptationStatus("idle");
    setDetailLoading(false);
    setDetailTab("overview");
    setStageMenuOpen(false);
    setPriorityMenuOpen(false);
    setJdExpanded(false);
    setBriefing("");
    setBriefingLoading(false);
    setPollMessage("");
    setPollTimedOut(false);
    setNoteDraft("");
    setNoteEditing(false);
    setDetailEditing(false);
    setProfileDraft(null);
    setProfileBase(null);
    setProfileConflict(null);
    setProfileDirty(false);
    setProfileSaving(false);
    setCurrentStepEditing(false);
    setCurrentStepDraft("");
    setCurrentStepSaving(false);
    setNextActionDraft(null);
    setCompletionDraft(null);
    setNextActionSaving(false);
    setDeleteOperationIds([]);
    setReviewUndoPreparingId(null);
    setReviewUndoOperationIds([]);
    setNoteExpected(null);
    setNoteExpectedRevision(null);
    setNoteConflict(null);
    setNoteDirty(false);
    setHistoryEditDrafts({});
    setHistoryDraft(null);
    setPendingHistoryDelete(null);
    setRememberHistoryDeleteChoice(false);
    setError("");
  }

  async function loadBriefing(id: number,
                              selectionEpoch = selectionEpochRef.current): Promise<boolean> {
    try {
      const locale = encodeURIComponent(currentOutputLocale());
      const r = await getJson<{ markdown: string }>(
        `/api/timeline/applications/${id}/briefing?locale=${locale}`,
      );
      if (selectedIdRef.current !== id || selectionEpochRef.current !== selectionEpoch) return false;
      setBriefing(r.markdown);
      return true;
    } catch (e) {
      if (selectedIdRef.current === id && selectionEpochRef.current === selectionEpoch) {
        if (e instanceof HttpError && e.status === 404) clearMissingApplication(id);
        else setError(e instanceof Error ? e.message : l("调研页加载失败", "Could not load research"));
      }
      return false;
    }
  }

  // UI and explicitly authorized agent actions share one generation command boundary.
  // Company and role reports plus suggested answers run in the background and are polled;
  // online company research runs only when the setting explicitly permits it.
  async function generatePrep(id: number, options: PrepGenerationOptions = {}): Promise<boolean> {
    const selectionEpoch = selectionEpochRef.current;
    const controller = new AbortController();
    const requestTimeout = window.setTimeout(() => controller.abort(), PREP_TRIGGER_TIMEOUT_MS);
    try {
      const params = new URLSearchParams();
      params.set("output_locale", currentOutputLocale());
      if (options.forceTakeover) params.set("force", "true");
      if (options.refreshResearch) params.set("refresh_research", "true");
      const query = params.size > 0 ? `?${params.toString()}` : "";
      const r = await postJson<PrepTriggerResponse>(
        `/api/timeline/applications/${id}/prep${query}`, {}, { signal: controller.signal });
      if (r.status === "error") {
        if (selectedIdRef.current === id && selectionEpochRef.current === selectionEpoch) {
          setError(r.message ?? l("调研生成失败", "Research generation failed"));
        }
        if (options.forceTakeover && selectedIdRef.current === id
            && selectionEpochRef.current === selectionEpoch) setPollTimedOut(true);
        return false;
      }
      if (selectedIdRef.current !== id || selectionEpochRef.current !== selectionEpoch) return false;
      if (r.status === "completed") {
        // Completed is an atomic server fact; settle locally so a failed detail refresh cannot leave a permanent running state.
        setDetail((previous) => previous?.id === id ? {
          ...previous,
          prep_status: r.prep_status ?? "ready",
          prep_retry_after_seconds: null,
        } : previous);
        setPollTimedOut(false);
        setPollMessage(r.message ?? l("原任务已结束，已加载最新结果。", "The original task has ended and the latest result is loaded."));
        const latest = await refresh(id, selectionEpoch);
        if (selectedIdRef.current !== id || selectionEpochRef.current !== selectionEpoch) return false;
        if (latest === null) {
          setPollMessage(l("任务已完成，但最新详情暂时加载失败；可稍后重新打开岗位。", "The task completed, but the latest details could not be loaded. Reopen the role later."));
        }
        if (briefingOpenRef.current) await loadBriefing(id, selectionEpoch);
        return true;
      }
      await refresh(id, selectionEpoch);
      if (selectedIdRef.current !== id || selectionEpochRef.current !== selectionEpoch) return false;
      const notice = r.status === "reused"
        ? (r.message ?? l("已有任务正在运行，本次没有重复调用模型。", "A task is already running; no duplicate model call was made."))
        : [
          r.takeover_applied ? l("已安全接管过期任务并重新开始生成。", "The expired task was safely taken over and generation restarted.") : "",
          r.message ?? "",
        ].filter(Boolean).join(" ");
      if (r.status === "reused" && (r.retry_after_seconds ?? 1) <= 0) {
        setPollTimedOut(true);
        setPollMessage(notice);
      } else {
        startPolling(id, notice);
      }
      return true;
    } catch (e) {
      if (selectedIdRef.current === id && selectionEpochRef.current === selectionEpoch) {
        if (e instanceof HttpError && e.status === 404) clearMissingApplication(id);
        else {
          setError(isAbortError(e) ? l("启动调研任务超时；任务可能已提交，可重试以查询当前状态", "Starting research timed out. The task may have been submitted; retry to check its status.")
            : e instanceof Error ? e.message : l("调研生成失败", "Research generation failed"));
          if (options.forceTakeover) setPollTimedOut(true);
        }
      }
      return false;
    } finally {
      clearTimeout(requestTimeout);
    }
  }

  // Open cached research immediately. Generate missing or previously failed enhancements in the background.
  async function openBriefing() {
    if (!detail || briefingLoading) return;
    const id = detail.id;
    const selectionEpoch = selectionEpochRef.current;
    const prepStatus = detail.prep_status;
    const request = ++briefingRequestRef.current;
    setBriefingLoading(true);   // Provide immediate feedback for the click.
    setError("");
    setDetailTab("research");
    try {
      const loaded = await loadBriefing(id, selectionEpoch);
      // The base page does not require a model; configuration or generation failures only affect enhancements.
      if (loaded && (prepStatus === "none" || prepStatus === "failed")) {
        await generatePrep(id);
      }
    } catch (e) {
      if (selectedIdRef.current === id && selectionEpochRef.current === selectionEpoch
          && briefingRequestRef.current === request) {
        setError(e instanceof Error ? e.message : l("调研生成失败", "Research generation failed"));
      }
    } finally {
      if (briefingRequestRef.current === request) setBriefingLoading(false);
    }
  }

  // Overview is immediate; research and adaptation are lazy-loaded without invoking a model on tab entry.
  function selectDetailTab(tab: "overview" | "research" | "adaptation") {
    setStageMenuOpen(false);
    setPriorityMenuOpen(false);
    if (detailEditing && tab !== "overview") return;   // Keep editing on Overview to avoid navigating away from a partial save.
    setDetailTab(tab);
    if (!detail) return;
    if (tab === "research" && briefing === "" && !briefingLoading
        && detail.prep_status !== "none" && detail.prep_status !== "failed") {
      void openBriefing();
    }
  }

  // Reuse the board's optimistic CAS path when the header stage pill changes the stage.
  function changeDetailStage(target: string) {
    setStageMenuOpen(false);
    setPriorityMenuOpen(false);
    if (!detail || detail.stage === target) return;
    if (detailRefreshBlockedRef.current) {
      setError(l(
        "还有正在编辑或未保存的详情；请先保存或取消，再修改阶段。",
        "Some role details are being edited or remain unsaved. Save or cancel them before changing the stage.",
      ));
      return;
    }
    void moveApplicationStage(detail, target as ApplicationStage, "detail_menu");
  }

  async function changePriority(priority: ApplicationPriority) {
    setPriorityMenuOpen(false);
    if (!detail || prioritySaving || detail.priority === priority || detailRefreshBlockedRef.current
        || pendingStageMovesRef.current.has(detail.id)) return;
    const currentDetail = detail;
    const id = currentDetail.id;
    const selectionEpoch = selectionEpochRef.current;
    setPrioritySaving(true);
    setError("");
    try {
      const loaded = ApplicationDetail.parse(await putJson<unknown>(
        `/api/timeline/applications/${id}/priority`, {
          expected_revision: currentDetail.revision,
          priority,
        },
      ));
      setBoard((current) => current === null ? null : patchBoardItem(current, id, {
        priority: loaded.priority,
        revision: loaded.revision,
      }));
      setUpcoming((current) => current.map((item) => item.id === id ? {
        ...item,
        priority: loaded.priority,
        revision: loaded.revision,
      } : item));
      if (selectedIdRef.current === id && selectionEpochRef.current === selectionEpoch) {
        setDetail(projectLoadedApplicationDetail(
          loaded,
          pendingStageMovesRef.current,
          settledStageOverridesRef.current,
        ));
      }
    } catch (e) {
      const stillSelected = (
        selectedIdRef.current === id && selectionEpochRef.current === selectionEpoch
      );
      if (e instanceof HttpError && e.status === 404) {
        clearMissingApplication(id);
      } else if (stillSelected) setError(e instanceof Error ? e.message : l("优先级保存失败", "Could not save priority"));
      if (e instanceof HttpError && e.status === 409) {
        await refresh(stillSelected ? id : undefined, selectionEpoch);
      }
    } finally {
      setPrioritySaving(false);
    }
  }

  function editCurrentStep() {
    if (!detail || detailRefreshBlockedRef.current
        || pendingStageMovesRef.current.has(detail.id)) return;
    setStageMenuOpen(false);
    setPriorityMenuOpen(false);
    setCurrentStepDraft(detail.current_step ?? "");
    setCurrentStepEditing(true);
    setError("");
  }

  async function saveCurrentStep() {
    if (!detail || currentStepSaving || !currentStepEditing) return;
    const currentDetail = detail;
    const id = currentDetail.id;
    const selectionEpoch = selectionEpochRef.current;
    const currentStep = currentStepDraft.trim();
    if (currentStep === (currentDetail.current_step ?? "")) {
      setCurrentStepEditing(false);
      return;
    }
    setCurrentStepSaving(true);
    setError("");
    try {
      const loaded = ApplicationDetail.parse(await putJson<unknown>(
        `/api/timeline/applications/${id}/profile`,
        {
          expected_revision: currentDetail.revision,
          company: currentDetail.company,
          position: currentDetail.position,
          department: currentDetail.department,
          channel: currentDetail.channel,
          stage: currentDetail.stage,
          current_step: currentStep || null,
          applied_date: currentDetail.applied_date,
          pause_reason: currentDetail.stage === "pooled" ? currentDetail.pause_reason : null,
          next_action: currentDetail.next_action,
          jd_text: currentDetail.jd_text,
        },
      ));
      setBoard((current) => current === null ? null : patchBoardItem(current, id, {
        current_step: loaded.current_step,
        revision: loaded.revision,
      }));
      setUpcoming((current) => current.map((item) => item.id === id ? {
        ...item,
        current_step: loaded.current_step,
        revision: loaded.revision,
      } : item));
      if (selectedIdRef.current === id && selectionEpochRef.current === selectionEpoch) {
        setDetail(loaded);
        setCurrentStepDraft(loaded.current_step ?? "");
        setCurrentStepEditing(false);
      }
    } catch (e) {
      const stillSelected = (
        selectedIdRef.current === id && selectionEpochRef.current === selectionEpoch
      );
      if (e instanceof HttpError && e.status === 404) {
        clearMissingApplication(
          id,
          recoveryText(
            l("未保存的当前环节", "Unsaved current step"),
            [[l("当前环节", "Current step"), currentStepDraft]],
            locale,
          ),
        );
      } else if (stillSelected) {
        setError(e instanceof HttpError && e.status === 409
          ? l("岗位刚刚在另一窗口更新；当前环节尚未保存，请核对后再退出一次。", "The role was just updated in another window. The current step was not saved; review it before leaving again.")
          : e instanceof Error ? e.message : l("当前环节保存失败", "Could not save current step"));
      }
      if (e instanceof HttpError && e.status === 409) {
        await loadDetail(
          id,
          selectionEpoch,
          undefined,
          true,
          recoveryText(
            l("未保存的当前环节", "Unsaved current step"),
            [[l("当前环节", "Current step"), currentStepDraft]],
            locale,
          ),
        );
      }
    } finally {
      setCurrentStepSaving(false);
    }
  }

  async function saveApplicationNote(
    nextNote = noteDraft,
    expectedRevision = noteExpectedRevision ?? detail?.revision ?? 0,
    exitEditing = true,
    missingRecovery?: MissingDraftRecovery,
  ): Promise<ApplicationDetail | null> {
    if (!detail || noteSaving) return null;
    const id = detail.id;
    setNoteSaving(true);
    setError("");
    try {
      const response = ApplicationDetail.parse(await putJson<unknown>(
        `/api/timeline/applications/${id}/note`,
        { note: nextNote.trim() || null, expected_revision: expectedRevision },
      ));
      setNoteDraft(response.application_note ?? "");
      setDetail((current) => current?.id === id ? response : current);
      setNoteDirty(false);
      setNoteEditing(!exitEditing);
      setNoteExpected(response.application_note);
      setNoteExpectedRevision(response.revision);
      setNoteConflict(null);
      await refresh(id, selectionEpochRef.current);
      return response;
    } catch (e) {
      if (e instanceof HttpError && e.status === 404) {
        clearMissingApplication(
          id,
          missingRecovery ?? recoveryText(
            l("未保存的岗位备注", "Unsaved role note"),
            [[l("备注", "Note"), nextNote]],
            locale,
          ),
        );
      } else setError(e instanceof Error ? e.message : l("岗位备注保存失败", "Could not save role notes"));
      if (e instanceof HttpError && e.status === 409) {
        // Preserve the draft and edit state while refreshing the authoritative note for manual merging.
        const loaded = await loadDetail(
          id,
          selectionEpochRef.current,
          undefined,
          true,
          missingRecovery ?? recoveryText(
            l("未保存的岗位备注", "Unsaved role note"),
            [[l("备注", "Note"), nextNote]],
            locale,
          ),
        );
        if (loaded) {
          setNoteConflict({ savedNote: loaded.application_note, rebased: false });
        }
      }
      return null;
    } finally {
      setNoteSaving(false);
    }
  }

  function editApplicationNote() {
    if (!detail) return;
    setNoteDraft(detail.application_note ?? "");
    setNoteExpected(detail.application_note);
    setNoteExpectedRevision(detail.revision);
    setNoteConflict(null);
    setNoteDirty(false);
    setNoteEditing(true);
  }

  function cancelApplicationNoteEdit() {
    setNoteDraft(detail?.application_note ?? "");
    setNoteDirty(false);
    setNoteEditing(false);
    setNoteExpected(null);
    setNoteExpectedRevision(null);
    setNoteConflict(null);
  }

  function changeCompletionOutcome(outcome: CompleteNextActionDraft["outcome"]) {
    if (!completionDraft) return;
    if (outcome === "failed" && completionDraft.outcome !== "failed" && !window.confirm(l(
      "结果设为“未通过”后，这个岗位会移到“已挂”，并清空后续安排。是否继续？",
      "Marking the result as Not passed moves this role to Rejected and clears future plans. Continue?",
    ))) return;
    setCompletionDraft({
      ...completionDraft,
      outcome,
      set_next_action: outcome === "failed" ? false : completionDraft.set_next_action,
    });
  }

  function startCompletingNextAction() {
    if (!detail?.next_action || nextActionSaving
        || pendingStageMovesRef.current.has(detail.id)) return;
    setCompletionDraft({
      expected_revision: detail.revision,
      expected_next_action: { ...detail.next_action },
      conflicted: false,
      occurred_date: today,
      outcome: "",
      summary: "",
      set_next_action: false,
      next_action: {
        stage: detail.next_action.stage,
        step: "",
        date: "",
        time: "",
        note: "",
      },
    });
    setError("");
  }

  async function completeNextAction() {
    if (!detail?.next_action || !completionDraft || nextActionSaving
        || pendingStageMovesRef.current.has(detail.id)) return;
    if (completionDraft.conflicted) {
      setError(l(
        "要完成的下一步已经变化；请放弃这份草稿，再按最新安排记录。",
        "The next action you were completing has changed. Discard this draft and record the latest action instead.",
      ));
      return;
    }
    if (!completionClosesApplication
        && completionDraft.set_next_action
        && !completionDraft.next_action.step.trim()) {
      setError(l(
        "继续安排下一步时必须填写下一步名称。",
        "Enter a name when scheduling another next action.",
      ));
      return;
    }
    if (!completionClosesApplication
        && completionDraft.set_next_action
        && completionDraft.next_action.time
        && !completionDraft.next_action.date) {
      setError(l(
        "设置具体时间前，请先选择日期。",
        "Choose a date before setting a specific time.",
      ));
      return;
    }
    const applicationId = detail.id;
    setNextActionSaving(true);
    setError("");
    try {
      const loaded = ApplicationDetail.parse(await postJson<unknown>(
        `/api/timeline/applications/${applicationId}/complete-next-action`,
        {
          expected_revision: completionDraft.expected_revision,
          occurred_date: completionDraft.occurred_date || null,
          outcome: completionDraft.outcome || null,
          summary: completionDraft.summary.trim() || null,
          next_action: !completionClosesApplication && completionDraft.set_next_action ? {
            stage: completionDraft.next_action.stage,
            step: completionDraft.next_action.step.trim(),
            date: completionDraft.next_action.date || null,
            time: completionDraft.next_action.time || null,
            note: completionDraft.next_action.note.trim() || null,
          } : null,
        },
      ));
      setDetail(loaded);
      setNextActionDraft(null);
      setCompletionDraft(null);
      setHistoryEditDrafts(historyDraftsFromEntries(loaded.timeline_entries, loaded.revision));
      await refresh(applicationId, selectionEpochRef.current);
    } catch (caught) {
      if (caught instanceof HttpError && caught.status === 404) {
        clearMissingApplication(applicationId, recoveryText(
          l("未保存的完成记录", "Unsaved completion record"),
          [
            [l("完成项目", "Completed action"), completionDraft.expected_next_action.step],
            [l("完成日期", "Completion date"), completionDraft.occurred_date],
            [l("结果", "Outcome"), completionDraft.outcome ? outcomeLabel(completionDraft.outcome, locale) : ""],
            [l("说明", "Notes"), completionDraft.summary],
            [l("后续下一步", "Following action"), completionDraft.set_next_action ? completionDraft.next_action.step : ""],
            [l("后续日期", "Following date"), completionDraft.set_next_action ? completionDraft.next_action.date : ""],
            [l("后续说明", "Following notes"), completionDraft.set_next_action ? completionDraft.next_action.note : ""],
          ],
          locale,
        ));
      } else setError(caught instanceof Error ? caught.message : l("完成下一步失败", "Could not complete the next action"));
      if (caught instanceof HttpError && caught.status === 409) {
        const authoritative = await loadDetail(
          applicationId,
          selectionEpochRef.current,
          undefined,
          true,
          recoveryText(l("未保存的完成记录", "Unsaved completion record"), [
            [l("完成项目", "Completed action"), completionDraft.expected_next_action.step],
            [l("完成日期", "Completion date"), completionDraft.occurred_date],
            [l("结果", "Outcome"), completionDraft.outcome ? outcomeLabel(completionDraft.outcome, locale) : ""],
            [l("说明", "Notes"), completionDraft.summary],
            [l("后续下一步", "Following action"), completionDraft.set_next_action
              ? completionDraft.next_action.step
              : ""],
            [l("后续日期", "Following date"), completionDraft.set_next_action
              ? completionDraft.next_action.date
              : ""],
            [l("后续说明", "Following notes"), completionDraft.set_next_action
              ? completionDraft.next_action.note
              : ""],
          ], locale),
        );
        if (authoritative && sameNextAction(
          authoritative.next_action,
          completionDraft.expected_next_action,
        )) {
          setCompletionDraft((current) => current ? {
            ...current,
            expected_revision: authoritative.revision,
            expected_next_action: { ...completionDraft.expected_next_action },
            conflicted: false,
          } : current);
          setError(l(
            "岗位的其他信息刚刚更新；完成草稿已安全换到最新版本，请再次确认。",
            "Other role information just changed. The completion draft was safely rebased; confirm it again.",
          ));
        } else if (authoritative) {
          setCompletionDraft((current) => current ? { ...current, conflicted: true } : current);
          setError(l(
            "要完成的下一步已在 Agent 或另一窗口中变化；草稿未写入。",
            "The next action changed in an agent or another window. Your draft was not saved.",
          ));
        }
      }
    } finally {
      setNextActionSaving(false);
    }
  }

  function startDetailEdit() {
    if (!detail || detailRefreshBlockedRef.current
        || pendingStageMovesRef.current.has(detail.id)) return;
    setStageMenuOpen(false);
    setDetailTab("overview");
    const initialProfile = profileDraftFromDetail(detail);
    setProfileDraft(initialProfile);
    setProfileBase(initialProfile);
    setProfileConflict(null);
    setProfileDirty(false);
    setDetailEditing(true);
    setNextActionDraft(nextActionDraftFromDetail(detail));
    setCompletionDraft(null);
    editApplicationNote();
    setHistoryEditDrafts(historyDraftsFromEntries(detail.timeline_entries, detail.revision));
    setHistoryDraft(null);
    setPendingHistoryDelete(null);
    setError("");
  }

  function cancelDetailEdit() {
    if (profileSaving || noteSaving || historySaving) return;
    setDetailEditing(false);
    setProfileDraft(null);
    setProfileBase(null);
    setProfileConflict(null);
    setProfileDirty(false);
    setNextActionDraft(null);
    setCompletionDraft(null);
    cancelApplicationNoteEdit();
    setHistoryEditDrafts({});
    setHistoryDraft(null);
    setPendingHistoryDelete(null);
    setRememberHistoryDeleteChoice(false);
    setError("");
  }

  async function saveDetailEdit() {
    if (!detail || !profileDraft || profileSaving || noteSaving || historySaving) return;
    if (!nextActionDraft) {
      setError(l(
        "下一步安排草稿没有正确初始化，请取消编辑后重试。",
        "The next-action draft was not initialized correctly. Cancel editing and try again.",
      ));
      return;
    }
    if (nextActionDraft.conflicted) {
      setError(l(
        "下一步已在 Agent 或另一窗口中变化；请先选择使用最新安排，再保存整页。",
        "The next action changed in an agent or another window. Choose the latest plan before saving the page.",
      ));
      return;
    }
    if (nextActionDraftInvalid) {
      setError(nextActionDraft.time && !nextActionDraft.date
        ? l("设置下一步具体时间前，请先选择日期。", "Choose a date before setting a specific time for the next action.")
        : l("填写了下一步日期或说明时，也必须填写下一步名称；如需删除，请点击“清除安排”。", "A dated or annotated next action also needs a name. To remove it, choose Clear action."));
      return;
    }
    if (codePointLength(noteDraft) > 2000) {
      setError(l(
        "岗位备注不能超过 2000 字；请精简后再保存。",
        "Role notes cannot exceed 2,000 characters. Shorten them before saving.",
      ));
      return;
    }
    if (noteConflict && !noteConflict.rebased) {
      setError(l(
        "岗位备注已在另一窗口变化；请先在备注区域选择合并方式，再保存整页。",
        "Role notes changed in another window. Choose how to merge them before saving the page.",
      ));
      return;
    }
    if (historyDraft !== null) {
      setError(l(
        "请先保存或取消新添加的历程，再完成整页编辑。",
        "Save or cancel the new history entry before finishing the page edit.",
      ));
      return;
    }
    const company = profileDraft.company.trim();
    const position = profileDraft.position.trim();
    if (!company || !position) {
      setError(l(
        "公司名称和岗位名称不能为空。",
        "Company and role names are required.",
      ));
      return;
    }
    const desiredNextAction = stageEndsApplication(profileDraft.stage)
      ? null
      : nextActionFromDraft(nextActionDraft);
    const nextActionDirty = !sameNextAction(
      desiredNextAction,
      nextActionDraft.base_next_action,
    );
    const id = detail.id;
    const dirtyHistoryDrafts = detail.timeline_entries.flatMap((entry) => {
      const draft = historyEditDrafts[entry.id];
      return historyDraftChanged(draft, entry) && draft ? [draft] : [];
    });
    if (dirtyHistoryDrafts.some((draft) => draft.conflicted)) {
      setError(l("请先处理历程中与另一窗口冲突的字段，再完成整页保存。", "Resolve history fields that conflict with another window before saving the page."));
      return;
    }
    if (dirtyHistoryDrafts.some((draft) => !historyEntryIsMeaningful({
      ...draft,
      from_stage: draft.projected_stage,
      to_stage: draft.projected_stage,
    }))) {
      setError(l("历程至少填写具体环节、结果、说明或阶段/环节变化。", "A history entry needs at least a step, outcome, note, or stage/step change."));
      return;
    }
    if (profileConflict && !profileConflict.acknowledged) {
      setError(l("请先检查并确认另一窗口也修改过的字段，再继续保存。", "Review and confirm the fields changed in another window before saving."));
      return;
    }
    if (profileDirty && stageEndsApplication(profileDraft.stage) && detail.next_action
        && !window.confirm(
          l(`把当前阶段改为「${stageLabel(profileDraft.stage)}」会清空下一步${formatNextActionForImpact(detail.next_action)}。是否保存？`, `Changing the current stage to “${stageLabel(profileDraft.stage, locale)}” clears the next action. Save anyway?`),
        )) {
      setError(l("已取消保存；下一步安排仍保留。", "Save cancelled; the next action is preserved."));
      return;
    }
    const detailEditRecovery = recoveryText(l(
      "未保存的岗位编辑草稿",
      "Unsaved role-edit draft",
    ), [
      [l("公司", "Company"), company],
      [l("岗位", "Role"), position],
      [l("部门", "Department"), profileDraft.department],
      [l("渠道", "Channel"), profileDraft.channel],
      [l("当前阶段", "Current stage"), stageLabel(profileDraft.stage, locale)],
      [l("当前环节", "Current step"), profileDraft.current_step],
      [l("投递日期", "Application date"), profileDraft.applied_date],
      [l("泡池原因", "On-hold reason"), profileDraft.stage === "pooled" ? profileDraft.pause_reason : ""],
      [l("岗位描述", "Job description"), profileDraft.jd_text],
      [l("下一步", "Next action"), desiredNextAction?.step],
      [l("下一步完成后阶段", "Stage after next action"), desiredNextAction ? stageLabel(desiredNextAction.stage, locale) : ""],
      [l("下一步日期", "Next-action date"), desiredNextAction?.date],
      [l("下一步时间", "Next-action time"), desiredNextAction?.time],
      [l("下一步说明", "Next-action notes"), desiredNextAction?.note],
      [l("备注", "Notes"), noteDirty ? noteDraft : ""],
      [l("历程修改", "History changes"), dirtyHistoryDrafts.map((draft) => [
        draft.occurred_date,
        draft.step,
        draft.outcome ? outcomeLabel(draft.outcome, locale) : "",
        draft.summary,
      ].filter(Boolean).join(" · ")).join("\n")],
    ], locale);
    setProfileSaving(true);
    if (dirtyHistoryDrafts.length > 0) setHistorySaving(true);
    setError("");
    let profileWritePending = profileDirty || nextActionDirty;
    let historyWritePending: HistoryDraft | null = null;
    try {
      let savedDetail = detail;
      if (profileDirty || nextActionDirty) {
        savedDetail = ApplicationDetail.parse(await putJson<unknown>(
          `/api/timeline/applications/${id}/profile`,
          {
            expected_revision: profileDraft.expected_revision,
            company,
            position,
            department: profileDraft.department.trim() || null,
            channel: profileDraft.channel.trim() || null,
            stage: profileDraft.stage,
            current_step: profileDraft.current_step.trim() || null,
            applied_date: profileDraft.applied_date || null,
            pause_reason: profileDraft.stage === "pooled"
              ? profileDraft.pause_reason.trim() || null
              : null,
            next_action: desiredNextAction,
            jd_text: profileDraft.jd_text.trim() || null,
          },
        ));
        profileWritePending = false;
        setDetail(savedDetail);
        const savedProfile = profileDraftFromDetail(savedDetail);
        setProfileDraft(savedProfile);
        setProfileBase(savedProfile);
        setProfileConflict(null);
        setProfileDirty(false);
        setNextActionDraft(nextActionDraftFromDetail(savedDetail));
      }
      if (noteDirty) {
        const noteSaved = await saveApplicationNote(
          noteDraft,
          savedDetail.revision,
          false,
          detailEditRecovery,
        );
        if (!noteSaved) return;
        savedDetail = noteSaved;
      }
      for (const draft of dirtyHistoryDrafts) {
        historyWritePending = draft;
        savedDetail = ApplicationDetail.parse(await putJson<unknown>(
          `/api/timeline/applications/${id}/timeline-entries/${draft.entryId}`,
          {
            expected_revision: savedDetail.revision,
            expected_fingerprint: draft.expected_fingerprint,
            step: draft.step.trim() || null,
            occurred_date: draft.occurred_date || null,
            outcome: draft.outcome || null,
            summary: draft.summary.trim() || null,
          },
        ));
        historyWritePending = null;
      }
      setDetail(savedDetail);
      setDetailEditing(false);
      setProfileDraft(null);
      setProfileBase(null);
      setProfileConflict(null);
      setNoteEditing(false);
      setNoteExpected(null);
      setNoteExpectedRevision(null);
      setNextActionDraft(null);
      setCompletionDraft(null);
      setHistoryEditDrafts({});
      await refresh(id, selectionEpochRef.current);
    } catch (e) {
      if (e instanceof HttpError && e.status === 404 && historyWritePending) {
        const loaded = await reconcileMissingTimelineEntry(
          id,
          l(
            "正在编辑的历程已在另一窗口删除；岗位仍保留，其他最新详情已刷新。未保存内容可从下方复制。",
            "The history entry being edited was deleted in another window. The role remains, other details were refreshed, and you can copy unsaved content below.",
          ),
          detailEditRecovery,
        );
        if (loaded) {
          setHistoryEditDrafts(rebaseHistoryEditDraftsAgainstLatest(
            detail.timeline_entries,
            historyEditDrafts,
            loaded.timeline_entries,
            loaded.revision,
          ).drafts);
        }
      } else if (e instanceof HttpError && e.status === 404) {
        clearMissingApplication(id, detailEditRecovery);
      } else if (!(e instanceof HttpError && e.status === 409)) {
        setError(e instanceof Error ? e.message : l("岗位信息保存失败", "Could not save role information"));
      }
      if (e instanceof HttpError && e.status === 409 && historyWritePending) {
        const draftAtConflict = historyWritePending;
        const loaded = await loadDetail(
          id,
          selectionEpochRef.current,
          undefined,
          true,
          detailEditRecovery,
        );
        const latestEntry = loaded?.timeline_entries.find(
          (entry) => entry.id === draftAtConflict.entryId,
        );
        if (loaded && latestEntry) {
          const rebased = rebaseHistoryEditDraftsAgainstLatest(
            detail.timeline_entries,
            historyEditDrafts,
            loaded.timeline_entries,
            loaded.revision,
          );
          setHistoryEditDrafts(rebased.drafts);
          setError(rebased.outcomes[latestEntry.id] === "safe_rebase"
            ? l("这条历程或岗位的其他信息刚刚更新；草稿已与最新值安全合并，请再次保存。", "This history entry or other role information just changed. Your draft was safely merged; save again.")
            : l("这条历程的同一字段已在另一窗口修改；最新值和你的草稿均已保留，请明确选择后再保存。", "The same history field changed in another window. Both the latest value and your draft are preserved; choose explicitly before saving."));
        } else if (loaded) {
          setHistoryEditDrafts(historyDraftsFromEntries(
            loaded.timeline_entries,
            loaded.revision,
          ));
          setMissingDraftRecovery(detailEditRecovery.text ? detailEditRecovery : null);
          setExternalChangeNotice(l(
            "正在编辑的历程已在另一窗口删除；岗位仍保留，未保存内容可从下方复制。",
            "The history entry being edited was deleted in another window. The role remains and you can copy unsaved content below.",
          ));
        }
      } else if (e instanceof HttpError && e.status === 409 && profileWritePending) {
        const draftAtConflict = profileDraft;
        const baseAtConflict = profileBase ?? profileDraftFromDetail(detail);
        const loaded = await loadDetail(
          id,
          selectionEpochRef.current,
          undefined,
          true,
          detailEditRecovery,
        );
        if (loaded) {
          const latestProfile = profileDraftFromDetail(loaded);
          if (latestProfile.expected_revision === baseAtConflict.expected_revision) {
            // Business 409s such as name collisions are not version drift; preserve the error and draft.
            setProfileBase(latestProfile);
            setProfileConflict(null);
            setError(e.message || l("岗位信息与现有记录冲突；草稿已保留，请调整后重试。", "The role information conflicts with an existing record. Your draft is preserved; adjust it and retry."));
            return;
          }
          const merged = mergeConflictingApplicationProfile(
            baseAtConflict,
            draftAtConflict,
            latestProfile,
          );
          const nextActionConflict = nextActionDirty && !sameNextAction(
            loaded.next_action,
            nextActionDraft.base_next_action,
          );
          if (nextActionConflict) {
            setNextActionDraft((current) => current ? { ...current, conflicted: true } : current);
          } else if (nextActionDirty) {
            setNextActionDraft((current) => current ? {
              ...current,
              expected_revision: loaded.revision,
              base_next_action: cloneNextAction(loaded.next_action),
              conflicted: false,
            } : current);
          } else {
            setNextActionDraft(nextActionDraftFromDetail(loaded));
          }
          setProfileDraft(merged.draft);
          setProfileBase(latestProfile);
          setProfileDirty(timelineProfileDraftChanged(merged.draft, latestProfile));
          setProfileConflict({
            fields: merged.conflictingFields,
            acknowledged: merged.conflictingFields.length === 0,
          });
          setError(nextActionConflict
            ? l("下一步已在 Agent 或另一窗口中变化；你的整页草稿仍保留，请先核对最新安排。", "The next action changed in an agent or another window. Your full-page draft is preserved; review the latest action first.")
            : l("岗位刚刚在另一窗口更新；未冲突字段已安全合并，请核对后再次保存。", "The role was just updated in another window. Non-conflicting fields were merged safely; review and save again."));
        }
      } else if (e instanceof HttpError && e.status === 409) {
        await loadDetail(
          id,
          selectionEpochRef.current,
          undefined,
          true,
          detailEditRecovery,
        );
      }
    } finally {
      setProfileSaving(false);
      setHistorySaving(false);
    }
  }

  async function prepareDetailDelete() {
    if (!detail || deletePreparing || detailRefreshBlockedRef.current) return;
    setDeletePreparing(true);
    setError("");
    try {
      const operation = await prepareApplicationDeleteOperation(detail.id);
      setDeleteOperationIds([operation.operation_id]);
      setDeleteRefreshSignal((current) => current + 1);
    } catch (e) {
      if (e instanceof HttpError && e.status === 404) clearMissingApplication(detail.id);
      else setError(e instanceof Error ? e.message : l("岗位删除预览生成失败", "Could not prepare the deletion preview"));
    } finally {
      setDeletePreparing(false);
    }
  }

  async function prepareReviewEntryUndo(entry: TimelineEntry) {
    if (!detail || entry.source !== "review" || reviewUndoPreparingId !== null) return;
    const draft = historyEditDrafts[entry.id];
    if (historyDraftChanged(draft, entry)) {
      setError(l("这条复盘历程还有未保存的修改；请先保存或还原，再发起完整撤销。", "This review history entry has unsaved changes. Save or revert them before undoing the full review."));
      return;
    }
    setReviewUndoPreparingId(entry.id);
    setError("");
    try {
      const operation = await prepareTimelineReviewUndoOperation(
        detail.id,
        entry.id,
        entry.snapshot_fingerprint,
      );
      setReviewUndoOperationIds([operation.operation_id]);
      setReviewUndoRefreshSignal((current) => current + 1);
    } catch (caught) {
      if (caught instanceof HttpError && caught.status === 404) {
        const loaded = await reconcileMissingTimelineEntry(
          detail.id,
          l(
            "这条复盘历程已在另一窗口删除或变化；岗位仍保留，详情已刷新。",
            "This review history entry was deleted or changed in another window. The role remains and its details were refreshed.",
          ),
        );
        if (loaded) {
          setHistoryEditDrafts(rebaseHistoryEditDraftsAgainstLatest(
            detail.timeline_entries,
            historyEditDrafts,
            loaded.timeline_entries,
            loaded.revision,
          ).drafts);
        }
      } else setError(caught instanceof Error ? caught.message : l("复盘撤销预览生成失败", "Could not prepare the review undo preview"));
      if (caught instanceof HttpError && caught.status === 409) {
        const loaded = await loadDetail(detail.id, selectionEpochRef.current, undefined, true);
        if (loaded) {
          setHistoryEditDrafts(rebaseHistoryEditDraftsAgainstLatest(
            detail.timeline_entries,
            historyEditDrafts,
            loaded.timeline_entries,
            loaded.revision,
          ).drafts);
          setError(l(
            "这条复盘历程已在另一窗口变化；岗位详情和可编辑字段已刷新，请重新核对。",
            "This review history entry changed in another window. Role details and editable fields were refreshed; check them again.",
          ));
        }
      }
    } finally {
      setReviewUndoPreparingId(null);
    }
  }

  async function moveApplicationStage(
    item: BoardItem,
    targetStage: ApplicationStage,
    origin: "board_drag" | "detail_menu",
  ) {
    if (item.stage === targetStage || pendingStageMovesRef.current.has(item.id)) return;
    if (stageEndsApplication(targetStage)
        && item.next_action !== null
        && !window.confirm(
          l(`移动到「${stageLabel(targetStage)}」会清空下一步${formatNextActionForImpact(item.next_action)}。是否继续？`, `Moving to “${stageLabel(targetStage, locale)}” clears the next action. Continue?`),
        )) return;
    const pending = { expectedStage: item.stage, targetStage };
    const boardSnapshot = board ? captureBoardItem(board.columns, item.id) : null;
    const upcomingSnapshot = captureListItem(upcoming, item.id);
    const detailSnapshot = detail?.id === item.id ? {
      stage: detail.stage,
      next_action: cloneNextAction(detail.next_action),
    } : null;
    const optimisticItem = projectStageMove(item, targetStage);
    const previousNoticeTimer = stageMoveNoticeTimersRef.current.get(item.id);
    if (previousNoticeTimer !== undefined) window.clearTimeout(previousNoticeTimer);
    stageMoveNoticeTimersRef.current.delete(item.id);
    setStageMoveNotices((current) => {
      if (!(item.id in current)) return current;
      const next = { ...current };
      delete next[item.id];
      return next;
    });
    pendingStageMovesRef.current.set(item.id, pending);
    setStageMovePending((current) => new Set(current).add(item.id));
    setBoard((current) => current
      ? patchBoardItem(moveBoardItem(current, item.id, targetStage), item.id, {
          stage: optimisticItem.stage,
          next_action: optimisticItem.next_action,
        })
      : current);
    setUpcoming((current) => current.map((candidate) => candidate.id === item.id
      ? projectStageMove(candidate, targetStage)
      : candidate));
    setDetail((current) => current?.id === item.id
      ? projectStageMove(current, targetStage)
      : current);
    setError("");
    try {
      const response = ApplicationDetail.parse(await putJson<unknown>(
        `/api/timeline/applications/${item.id}/stage`, {
          expected_revision: item.revision,
          stage: targetStage,
          origin,
        },
      ));
      settledStageOverridesRef.current.set(item.id, {
        detail: response,
      });
      boardRefreshEpochRef.current += 1;
      setBoard((current) => {
        if (!current) return current;
        const positioned = moveBoardItem(current, item.id, response.stage);
        return patchBoardItem(positioned, item.id, {
          stage: response.stage,
          current_step: response.current_step,
          next_action: response.next_action,
          revision: response.revision,
          applied_date: response.applied_date,
          pause_reason: response.pause_reason,
          paused_from_stage: response.paused_from_stage,
        });
      });
      setUpcoming((current) => current.map((candidate) => candidate.id === item.id
        ? {
            ...candidate,
            stage: response.stage,
            current_step: response.current_step,
            next_action: response.next_action,
            revision: response.revision,
            applied_date: response.applied_date,
            pause_reason: response.pause_reason,
            paused_from_stage: response.paused_from_stage,
          }
        : candidate));
      setDetail((current) => current?.id === item.id ? response : current);
      pendingStageMovesRef.current.delete(item.id);
      setStageMovePending((current) => {
        const next = new Set(current);
        next.delete(item.id);
        return next;
      });
      setStageMoveNotices((current) => ({
        ...current,
        [item.id]: {
          kind: "saved",
          message: l("已保存", "Saved"),
        },
      }));
      stageMoveNoticeTimersRef.current.set(item.id, window.setTimeout(() => {
        setStageMoveNotices((current) => {
          if (!(item.id in current)) return current;
          const next = { ...current };
          delete next[item.id];
          return next;
        });
        stageMoveNoticeTimersRef.current.delete(item.id);
      }, STAGE_MOVE_NOTICE_TTL_MS));
      // A pooled job is intentionally absent from the upcoming endpoint.  When it is
      // resumed, mapping the existing client list cannot reinsert its retained plan;
      // re-read the authoritative board + seven-day window immediately.  refresh's
      // epoch and settled override prevent an older in-flight read from undoing this
      // successful projection.
      const selected = selectedIdRef.current === item.id ? item.id : undefined;
      void refresh(selected, selectionEpochRef.current);
    } catch (e) {
      pendingStageMovesRef.current.delete(item.id);
      setStageMovePending((current) => {
        const next = new Set(current);
        next.delete(item.id);
        return next;
      });
      if (e instanceof HttpError && e.status === 404) {
        setStageMoveNotices((current) => {
          if (!(item.id in current)) return current;
          const next = { ...current };
          delete next[item.id];
          return next;
        });
        clearMissingApplication(item.id);
        return;
      }
      // On failure, restore the per-item snapshot without relying on refresh or overwriting concurrent changes.
      setBoard((current) => current && boardSnapshot ? {
        ...current,
        columns: restoreBoardItem(current.columns, boardSnapshot),
      } : current);
      setUpcoming((current) => restoreListItem(current, item.id, upcomingSnapshot));
      setDetail((current) => current?.id === item.id && detailSnapshot ? {
        ...current,
        stage: detailSnapshot.stage,
        next_action: cloneNextAction(detailSnapshot.next_action),
      } : current);
      setStageMoveNotices((current) => ({
        ...current,
        [item.id]: {
          kind: "error",
          message: l("失败请重试", "Failed—try again"),
        },
      }));
      stageMoveNoticeTimersRef.current.set(item.id, window.setTimeout(() => {
        setStageMoveNotices((current) => {
          if (!(item.id in current)) return current;
          const next = { ...current };
          delete next[item.id];
          return next;
        });
        stageMoveNoticeTimersRef.current.delete(item.id);
      }, STAGE_MOVE_NOTICE_TTL_MS));
      const selected = selectedIdRef.current === item.id ? item.id : undefined;
      await refresh(selected, selectionEpochRef.current);
    }
  }

  function startCreatingHistoryEntry() {
    setPendingHistoryDelete(null);
    setRememberHistoryDeleteChoice(false);
    setHistoryDraft({
      expected_revision: detail?.revision ?? 0,
      entryId: null,
      expected_fingerprint: null,
      step: "",
      occurred_date: today,
      outcome: "",
      summary: "",
      update_current_state: false,
      projected_stage: detail?.stage ?? "backlog",
      set_next_action: false,
      next_stage: detail?.stage ?? "backlog",
      next_step: "",
      next_date: "",
      next_time: "",
      next_note: "",
      conflicted: false,
      entry_conflict: null,
    });
  }

  async function saveHistoryEntry() {
    if (!detail || !historyDraft || historyDraft.entryId !== null || historySaving) return;
    if (historyDraft.conflicted) {
      setError(l(
        "岗位状态已更新；请先确认这份草稿仍应作用于最新阶段和下一步，再保存。",
        "The role status changed. Confirm that this draft still applies to the latest stage and next action before saving.",
      ));
      return;
    }
    if (!historyEntryIsMeaningful({
      ...historyDraft,
      from_stage: detail.stage,
      to_stage: historyDraft.update_current_state ? historyDraft.projected_stage : detail.stage,
    })) {
      setError(l(
        "历程至少填写具体环节、结果、说明或阶段/环节变化。",
        "A history entry needs at least a step, outcome, note, or stage/step change.",
      ));
      return;
    }
    const projectedStage = historyDraft.update_current_state
      ? historyDraft.projected_stage
      : detail.stage;
    const setNextAction = historyDraft.set_next_action
      && !TERMINAL_STAGES.includes(projectedStage);
    if (setNextAction && !historyDraft.next_step.trim()) {
      setError(l(
        "设置下一步时必须填写下一步名称。",
        "Enter a name when setting a next action.",
      ));
      return;
    }
    if (setNextAction && historyDraft.next_time && !historyDraft.next_date) {
      setError(l(
        "设置下一步具体时间前，请先选择日期。",
        "Choose a date before setting a specific time for the next action.",
      ));
      return;
    }
    const applicationId = detail.id;
    const payload = {
      expected_revision: historyDraft.expected_revision,
      step: historyDraft.step.trim() || null,
      occurred_date: historyDraft.occurred_date || null,
      outcome: historyDraft.outcome || null,
      summary: historyDraft.summary.trim() || null,
      update_current_state: historyDraft.update_current_state,
      target_stage: historyDraft.update_current_state ? historyDraft.projected_stage : null,
      target_step: historyDraft.update_current_state
        ? historyDraft.step.trim() || detail.current_step
        : null,
      next_action: setNextAction ? {
        stage: historyDraft.next_stage,
        step: historyDraft.next_step.trim(),
        date: historyDraft.next_date || null,
        time: historyDraft.next_time || null,
        note: historyDraft.next_note.trim() || null,
      } satisfies NextAction : undefined,
    };
    setHistorySaving(true);
    setError("");
    try {
      const loaded = ApplicationDetail.parse(await postJson<unknown>(
        `/api/timeline/applications/${applicationId}/progress`,
        payload,
      ));
      setHistoryDraft(null);
      setDetail(loaded);
      setHistoryEditDrafts(historyDraftsFromEntries(loaded.timeline_entries, loaded.revision));
      void refresh();
    } catch (e) {
      if (e instanceof HttpError && e.status === 404) {
        clearMissingApplication(applicationId, recoveryText(
          l("未保存的历程草稿", "Unsaved history draft"),
          [
          [l("发生日期", "Date"), historyDraft.occurred_date],
          [l("具体环节", "Step"), historyDraft.step],
          [l("结果", "Outcome"), historyDraft.outcome ? outcomeLabel(historyDraft.outcome, locale) : ""],
          [l("说明", "Notes"), historyDraft.summary],
          [l("更新后的阶段", "Updated stage"), historyDraft.update_current_state
            ? stageLabel(historyDraft.projected_stage, locale)
            : ""],
          [l("下一步", "Next action"), historyDraft.set_next_action ? historyDraft.next_step : ""],
          [l("下一步日期", "Next-action date"), historyDraft.set_next_action ? historyDraft.next_date : ""],
          [l("下一步说明", "Next-action notes"), historyDraft.set_next_action ? historyDraft.next_note : ""],
          ],
          locale,
        ));
      } else setError(e instanceof Error ? e.message : l("历程保存失败", "Could not save history entry"));
      if (e instanceof HttpError && e.status === 409) {
        const authoritative = await loadDetail(
          applicationId,
          selectionEpochRef.current,
          undefined,
          true,
          recoveryText(l("未保存的历程草稿", "Unsaved history draft"), [
            [l("发生日期", "Date"), historyDraft.occurred_date],
            [l("具体环节", "Step"), historyDraft.step],
            [l("结果", "Outcome"), historyDraft.outcome ? outcomeLabel(historyDraft.outcome, locale) : ""],
            [l("说明", "Notes"), historyDraft.summary],
            [l("更新后的阶段", "Updated stage"), historyDraft.update_current_state
              ? stageLabel(historyDraft.projected_stage, locale)
              : ""],
            [l("下一步", "Next action"), historyDraft.set_next_action ? historyDraft.next_step : ""],
            [l("下一步日期", "Next-action date"), historyDraft.set_next_action ? historyDraft.next_date : ""],
            [l("下一步说明", "Next-action notes"), historyDraft.set_next_action ? historyDraft.next_note : ""],
          ], locale),
        );
        if (authoritative) {
          const resolution = rebaseHistoryDraftAfterConflict(
            historyDraft,
            authoritative.revision,
          );
          setHistoryDraft(resolution.draft);
          setError(resolution.kind === "safe_rebase"
            ? l("岗位的其他信息刚刚更新；这条纯事实历程已安全换到最新版本，请再次保存。", "Other role information just changed. This factual history entry was safely rebased; save it again.")
            : l("当前阶段或下一步已变化；草稿完整保留。请核对最新状态并明确确认后再保存。", "The current stage or next action changed. Your draft is intact; review the latest state and confirm before saving."));
        }
      }
    } finally {
      setHistorySaving(false);
    }
  }

  function requestHistoryEntryDelete(entry: TimelineEntry) {
    if (historySaving || entry.source === "review") return;
    if (skipManualHistoryDeleteConfirmation) {
      void performHistoryEntryDelete(entry);
      return;
    }
    setRememberHistoryDeleteChoice(false);
    setPendingHistoryDelete(entry);
  }

  function confirmHistoryEntryDelete() {
    if (!pendingHistoryDelete || historySaving) return;
    const entry = pendingHistoryDelete;
    if (rememberHistoryDeleteChoice) {
      persistSkipManualHistoryDeleteConfirmation();
      setSkipManualHistoryDeleteConfirmation(true);
    }
    setPendingHistoryDelete(null);
    setRememberHistoryDeleteChoice(false);
    void performHistoryEntryDelete(entry);
  }

  async function performHistoryEntryDelete(entry: TimelineEntry) {
    if (!detail || historySaving) return;
    const applicationId = detail.id;
    setHistorySaving(true);
    setError("");
    try {
      const loaded = ApplicationDetail.parse(await del<unknown>(
        `/api/timeline/applications/${applicationId}/timeline-entries/${entry.id}`
        + `?expected_revision=${detail.revision}`
        + `&expected_fingerprint=${encodeURIComponent(entry.snapshot_fingerprint)}`,
      ));
      setDetail(loaded);
      setHistoryEditDrafts(rebaseHistoryEditDraftsAgainstLatest(
        detail.timeline_entries,
        historyEditDrafts,
        loaded.timeline_entries,
        loaded.revision,
      ).drafts);
      await refresh(applicationId, selectionEpochRef.current);
    } catch (e) {
      if (e instanceof HttpError && e.status === 404) {
        const loaded = await reconcileMissingTimelineEntry(
          applicationId,
          l(
            "这条历程已在另一窗口删除；岗位仍保留，详情已刷新。",
            "This history entry was deleted in another window. The role remains and its details were refreshed.",
          ),
        );
        if (loaded) {
          setHistoryEditDrafts(rebaseHistoryEditDraftsAgainstLatest(
            detail.timeline_entries,
            historyEditDrafts,
            loaded.timeline_entries,
            loaded.revision,
          ).drafts);
        }
      } else setError(e instanceof Error ? e.message : l("历程删除失败", "Could not delete history entry"));
      if (e instanceof HttpError && e.status === 409) {
        const loaded = await loadDetail(
          applicationId,
          selectionEpochRef.current,
          undefined,
          true,
        );
        if (loaded) {
          setHistoryEditDrafts(rebaseHistoryEditDraftsAgainstLatest(
            detail.timeline_entries,
            historyEditDrafts,
            loaded.timeline_entries,
            loaded.revision,
          ).drafts);
          setError(l(
            "这条历程已在另一窗口变化；最新值和其他未保存草稿均已保留，请重新核对。",
            "This history entry changed in another window. The latest values and other unsaved drafts were preserved; check them again.",
          ));
        }
      }
    } finally {
      setHistorySaving(false);
    }
  }

  useEffect(() => {
    pageMountedRef.current = true;
    return () => {   // Leaving the page revokes polling and all in-flight refresh write permissions.
      pageMountedRef.current = false;
      boardRefreshEpochRef.current += 1;
      selectedIdRef.current = null;
      selectionEpochRef.current += 1;
      briefingRequestRef.current += 1;
      pollEpochRef.current += 1;
      if (pollRef.current !== null) clearTimeout(pollRef.current);
      for (const timer of stageMoveNoticeTimersRef.current.values()) {
        window.clearTimeout(timer);
      }
      stageMoveNoticeTimersRef.current.clear();
    };
  }, []);

  closeDetailRef.current = closeDetail;

  async function handleApplicationCreated(created: ApplicationDetail) {
    setCreateApplicationOpen(false);
    await refresh();
    await select(created.id);
  }

  function handleWorkbookPrepared(_operationId: string, skippedRows: number) {
    setWorkbookImportOpen(false);
    setIntakeRefreshSignal((current) => current + 1);
    if (skippedRows > 0) {
      setExternalChangeNotice(l(`已生成导入预览；${skippedRows} 行因缺少必要信息或无法安全识别而略过。`, `Import preview prepared; ${skippedRows} ${skippedRows === 1 ? "row was" : "rows were"} skipped because required data was missing or unsafe to interpret.`));
    }
  }

  async function openStatistics() {
    setStatisticsOpen(true);
    setStatisticsLoading(true);
    setStatistics(null);
    setStatisticsError("");
    try {
      const payload = await getJson<unknown>("/api/timeline/statistics", { cache: "no-store" });
      setStatistics(TimelineStatistics.parse(payload));
    } catch (statisticsLoadError) {
      setStatisticsError(
        statisticsLoadError instanceof Error ? statisticsLoadError.message : l("求职统计加载失败", "Could not load application statistics"),
      );
    } finally {
      setStatisticsLoading(false);
    }
  }

  if (!board) {
    return <p className="text-sm text-ink-3">{error || l("加载中…", "Loading…")}</p>;
  }

  const rows = sortListRows(
    columns.flatMap(([key]) => (stageFilter && stageFilter !== key ? [] : board.columns[key] ?? []))
      .filter((item) => matchesTimelineQuery(item, search)),
  );
  const dayGroups = upcomingDayGroups(
    upcoming.filter((item) => matchesTimelineQuery(item, search)),
    today,
    locale,
  );
  return (
    <div className="flex flex-col gap-5 md:h-full md:min-h-0">
      {/* Timeline owns its compact title and toolbar row. */}
      <div className="flex shrink-0 flex-wrap items-end gap-x-4 gap-y-3">
        <div className="min-w-0">
          <h1 className="text-[22px] font-semibold tracking-tight">{l("求职进展", "Application Tracker")}</h1>
          <button
            type="button"
            onClick={() => void openStatistics()}
            className="mt-2 inline-flex items-center gap-1.5 rounded-full border border-line bg-panel px-2.5 py-1 text-xs font-medium text-ink-2 shadow-[var(--shadow-card)] transition-colors hover:border-line-strong hover:text-ink"
          >
            <svg viewBox="0 0 16 16" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round">
              <path d="M2.5 13.5h11M4 11V7.5M8 11V3M12 11V5.5" />
            </svg>
            {l("数据统计", "Statistics")}
          </button>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-2">
          {board.total > 0 && (
            <>
              <div className="segmented">
                <button aria-pressed={view === "board"} onClick={() => setView("board")} className={`segmented-item ${view === "board" ? "segmented-on" : ""}`}>{l("看板", "Board")}</button>
                <button aria-pressed={view === "list"} onClick={() => setView("list")} className={`segmented-item ${view === "list" ? "segmented-on" : ""}`}>{l("列表", "List")}</button>
              </div>
              <div className="relative">
                <svg viewBox="0 0 16 16" className="pointer-events-none absolute left-2.5 top-1/2 h-3.5 w-3.5 -translate-y-1/2 text-ink-3" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round">
                  <circle cx="7" cy="7" r="4.2" />
                  <path d="m10.5 10.5 3 3" />
                </svg>
                <input
                  aria-label={l("搜索公司、岗位或渠道", "Search company, role, or source")}
                  value={search}
                  onChange={(e) => setSearch(e.target.value)}
                  placeholder={l("搜索公司、岗位或渠道", "Search company, role, or source")}
                  className="input w-52 !rounded-full !py-1 !pl-8"
                />
              </div>
            </>
          )}
          <button type="button" onClick={() => setWorkbookImportOpen(true)} className="btn btn-sm">
            <svg viewBox="0 0 16 16" className="h-3.5 w-3.5" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M3 2.5h7l3 3v8H3z" /><path d="M10 2.5v3h3M5.5 8h5M5.5 10.5h5" /></svg>
            {l("批量导入", "Bulk import")}
          </button>
          <button type="button" onClick={() => setCreateApplicationOpen(true)} className="btn-primary btn-sm">
            <span aria-hidden="true">＋</span> {l("新增岗位", "Add role")}
          </button>
        </div>
      </div>
      {externalChangeNotice && (
        <div role="status" className="rounded-xl bg-info-soft px-3 py-2 text-sm text-info">
          <div className="flex flex-wrap items-center justify-between gap-2">
            <span>{externalChangeNotice}</span>
            <button
              type="button"
              onClick={() => {
                setExternalChangeNotice("");
                setMissingDraftRecovery(null);
              }}
              className="inline-flex h-7 w-7 shrink-0 items-center justify-center rounded-lg text-lg leading-none transition-colors hover:bg-black/5 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-current"
              aria-label={l("关闭岗位变更提示", "Dismiss role-change notice")}
            >
              ×
            </button>
          </div>
          {missingDraftRecovery && (
            <div className="mt-2 rounded-lg border border-info/20 bg-panel/80 p-2.5 text-ink-2">
              <p className="text-xs font-semibold text-info">{missingDraftRecovery.title}{l("（可选择并复制）", " (select and copy)")}</p>
              <pre className="mt-1 max-h-36 overflow-auto whitespace-pre-wrap break-words select-text font-sans text-xs">
                {missingDraftRecovery.text}
              </pre>
            </div>
          )}
        </div>
      )}
      <IntakeOperationsPanel
        active
        refreshSignal={intakeRefreshSignal}
        onOperationSettled={() => { void refresh(selectedIdRef.current ?? undefined); }}
      />
      {/* Seven-day agenda grouped by date. */}
      {dayGroups.length > 0 && (
        upcomingCollapsed ? (
          <button
            type="button"
            aria-expanded="false"
            onClick={() => setUpcomingCollapsed(false)}
            className="flex w-full shrink-0 items-center justify-between rounded-xl border border-line-2 bg-panel px-4 py-2.5 text-left transition-colors hover:border-line-strong hover:bg-panel-2/40"
          >
            <span className="section-label">{l("接下来 7 天", "Next 7 days")}</span>
            <span aria-hidden="true" className="text-xs text-ink-3">↓</span>
          </button>
        ) : (
          <div className="shrink-0 rounded-2xl border border-line-2 bg-panel p-4">
            <p className="section-label mb-2.5">{l("接下来 7 天", "Next 7 days")}</p>
            <div id="timeline-upcoming-content" className="flex flex-col">
              {dayGroups.map((group) => (
                <div
                  key={group.date}
                  className="grid min-w-0 grid-cols-1 items-start gap-x-4 gap-y-1 border-t border-line-2 py-2 first:border-t-0 sm:grid-cols-[6.5rem_minmax(0,1fr)]"
                >
                  <span className="flex items-baseline gap-2 whitespace-nowrap leading-6">
                    <span className={`text-sm font-semibold ${group.isNear ? "text-warn" : ""}`}>{group.dow}</span>
                    <span className={`text-xs tabular-nums ${group.isNear ? "text-warn" : "text-ink-3"}`}>{group.dateLabel}</span>
                  </span>
                  <span className="flex min-w-0 flex-col">
                    {group.items.map((item) => (
                      <button
                        key={item.id}
                        onClick={() => void select(item.id)}
                        className="flex min-w-0 items-start gap-2 py-0.5 text-left text-sm leading-5 text-ink-2 transition-colors hover:text-ink"
                      >
                        <span className={`mt-2 h-1 w-1 shrink-0 rounded-full ${group.isNear ? "bg-warn" : "bg-ink-3"}`} />
                        <span className="min-w-0 break-words font-medium text-ink">{item.company}</span>
                        <span className="min-w-0 break-words">{upcomingActionLabel(item, locale)}</span>
                      </button>
                    ))}
                  </span>
                </div>
              ))}
            </div>
            <button
              type="button"
              aria-expanded="true"
              aria-controls="timeline-upcoming-content"
              onClick={() => setUpcomingCollapsed(true)}
              className="mt-2 flex w-full items-center justify-center gap-1 border-t border-line-2 pt-2 text-xs text-ink-3 transition-colors hover:text-ink"
            >
              {l("收起", "Collapse")} <span aria-hidden="true">↑</span>
            </button>
          </div>
        )
      )}

      {board.total === 0 && (
        <div className="card flex flex-col items-center gap-1 p-10 text-center">
          <svg viewBox="0 0 24 24" className="mb-2 h-8 w-8 text-ink-3" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
            <path d="M4 5h5v14H4zM10 5h5v9h-5zM16 5h4v14h-4z" />
          </svg>
          <p className="text-sm font-medium">{l("还没有岗位", "No roles yet")}</p>
          <p className="text-sm text-ink-3">{l("新增一个岗位，或让求职助手从 JD 中整理并导入。", "Add a role or ask Career Assistant to organise and import one from a job description.")}</p>
        </div>
      )}

      {board.total > 0 && (
        <>
          {view === "board" ? (
            <div className="md:min-h-0 md:flex-1">
              <TimelineBoard
                board={board}
                search={search}
                selectedId={selectedId}
                pendingIds={stageMovePending}
                notices={stageMoveNotices}
                onSelect={(id) => void select(id)}
                onMove={(item, stage) => void moveApplicationStage(item, stage, "board_drag")}
              />
            </div>
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-2">
                <button aria-pressed={stageFilter === null} onClick={() => setStageFilter(null)} className={`chip ${stageFilter === null ? "chip-on" : ""}`}>
                  {l("全部", "All")} · {board.total}
                </button>
                {columns.map(([key, label]) => {
                  const count = board.columns[key]?.length ?? 0;
                  if (count === 0) return null;
                  return (
                    <button
                      key={key}
                      aria-pressed={stageFilter === key}
                      onClick={() => setStageFilter(stageFilter === key ? null : key)}
                      className={`chip ${stageFilter === key ? "chip-on" : ""}`}
                    >
                      {label} · {count}
                    </button>
                  );
                })}
              </div>
              <div className="card overflow-hidden">
                <div className="flex items-center gap-3 border-b border-line-2 bg-panel-2/70 px-4 py-2 text-xs font-medium text-ink-3">
                  <span className="w-14 shrink-0">{l("优先级", "Priority")}</span>
                  <span className="w-40 shrink-0">{l("公司", "Company")}</span>
                  <span className="min-w-0 flex-1">{l("岗位", "Role")}</span>
                  <span className="hidden w-20 shrink-0 sm:block">{l("调研状态", "Research")}</span>
                  <span className="hidden w-24 shrink-0 text-right sm:block">{l("当前环节", "Current step")}</span>
                  <span className="hidden w-24 shrink-0 text-right md:block">{l("下一步日期", "Next date")}</span>
                  <span className="w-16 shrink-0 text-center">{l("阶段", "Stage")}</span>
                </div>
                {rows.length === 0 ? (
                  <p className="px-4 py-8 text-center text-sm text-ink-3">{l("没有匹配的岗位", "No matching roles")}</p>
                ) : (
                  rows.map((item, index) => {
                    const prep = prepMeta[item.prep_status];
                    return (
                      <button
                        key={item.id}
                        onClick={() => void select(item.id)}
                        className={`flex w-full items-center gap-3 px-4 py-3 text-left text-sm transition-colors hover:bg-panel-2 ${
                          index < rows.length - 1 ? "border-b border-line-2" : ""
                        } ${selectedId === item.id ? "bg-panel-2" : ""}`}
                      >
                        <span className="w-14 shrink-0">
                          {item.priority ? (
                            <span className={`tag justify-center ${PRIORITY_META[item.priority].badgeClass}`}>
                              {PRIORITY_META[item.priority].label}
                            </span>
                          ) : <span className="text-xs text-ink-3">—</span>}
                        </span>
                        <span className="flex w-40 shrink-0 items-center gap-1.5 truncate font-medium">
                          <span className="truncate">{item.company}</span>
                        </span>
                        <span className="min-w-0 flex-1 truncate text-ink-2">{item.position}</span>
                        <span className="hidden w-20 shrink-0 items-center gap-1.5 text-xs text-ink-3 sm:flex">
                          {prep && (
                            <>
                            <span className={`h-1.5 w-1.5 rounded-full ${prep[1]}`} />
                            {prep[0]}
                            </>
                          )}
                        </span>
                        <span className="hidden w-24 shrink-0 truncate text-right text-xs text-ink-3 sm:block">
                          {item.current_step ?? ""}
                        </span>
                        <span className="hidden w-24 shrink-0 items-center justify-end gap-1 text-xs tabular-nums text-warn md:flex">
                          {item.next_action?.date && !RAIL_STAGES.includes(item.stage) ? (
                            <>
                              <svg viewBox="0 0 16 16" className="h-3 w-3" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
                                <rect x="2.5" y="3.5" width="11" height="10" rx="1.5" /><path d="M2.5 6.5h11M5.5 2v2.5M10.5 2v2.5" />
                              </svg>
                              {item.next_action.date}
                            </>
                          ) : null}
                        </span>
                        <span className={`tag w-16 shrink-0 justify-center ${STAGE_STYLES[item.stage] ?? "bg-panel-2 text-ink-2"}`}>
                          {stageLabel(item.stage, locale)}
                        </span>
                      </button>
                    );
                  })
                )}
              </div>
            </>
          )}
        </>
      )}

      {createApplicationOpen && (
        <TimelineCreateApplicationDialog
          onCancel={() => setCreateApplicationOpen(false)}
          onCreated={handleApplicationCreated}
        />
      )}

      {workbookImportOpen && (
        <TimelineWorkbookImportDialog
          onCancel={() => setWorkbookImportOpen(false)}
          onPrepared={handleWorkbookPrepared}
        />
      )}

      {statisticsOpen && (
        <TimelineStatisticsDialog
          statistics={statistics}
          loading={statisticsLoading}
          error={statisticsError}
          onRetry={() => void openStatistics()}
          onClose={() => setStatisticsOpen(false)}
        />
      )}

      {/* Role details open in a modal drawer above the board. */}
      {selectedId !== null && (
        <div className="job-detail-layer fixed inset-0 z-40 flex justify-end" role="dialog" aria-modal="true" aria-label={l("岗位详情", "Role details")}>
          <button type="button" tabIndex={-1} aria-label={l("关闭岗位详情", "Close role details")} onClick={closeDetail} className="job-detail-backdrop absolute inset-0 cursor-default" />
          <aside
            ref={drawerRef}
            tabIndex={-1}
            className="job-detail-drawer relative z-10 flex flex-col overflow-hidden border border-line bg-panel outline-none"
          >
            {!detail ? (
              <div className="flex flex-1 items-center justify-center p-8">
                <p role="status" className="text-sm text-ink-3">
                  {detailLoading ? l("正在加载岗位详情…", "Loading role details…") : error || l("未找到岗位", "Role not found")}
                </p>
              </div>
            ) : (
              <>
                {(stageMenuOpen || priorityMenuOpen) && (
                  <button
                    type="button"
                    aria-hidden="true"
                    tabIndex={-1}
                    onClick={() => { setStageMenuOpen(false); setPriorityMenuOpen(false); }}
                    className="fixed inset-0 z-10 cursor-default"
                  />
                )}
                <div className="shrink-0 border-b border-line-2 px-4 pt-4 sm:px-6 sm:pt-5">
                  <div className="flex items-start gap-3">
                    <h2 className="min-w-0 flex-1 truncate text-lg font-semibold leading-7 tracking-tight">
                      <span>{detail.company}</span>
                      <span aria-hidden="true" className="font-normal text-ink-3"> · </span>
                      <span>{detail.position}</span>
                    </h2>
                    <button
                      type="button"
                      onClick={closeDetail}
                      aria-label={l("关闭岗位详情", "Close role details")}
                      className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-ink-3 transition-colors hover:bg-panel-2 hover:text-ink"
                    >
                      <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="m4 4 8 8M12 4l-8 8" /></svg>
                    </button>
                  </div>
                  <div className="mt-1.5 flex flex-wrap gap-x-3 gap-y-1 text-xs text-ink-3">
                    {detail.department && (
                      <span className="inline-flex items-baseline gap-1.5">
                        <span>{l("部门：", "Department:")}</span>
                        <span className="font-medium text-ink-2">{detail.department}</span>
                      </span>
                    )}
                    {detail.channel && (
                      <span className="inline-flex items-baseline gap-1.5">
                        <span>{l("渠道：", "Channel:")}</span>
                        <span className="font-medium text-ink-2">{detail.channel}</span>
                      </span>
                    )}
                    {detail.applied_date && <span className="tabular-nums">{l(`投递于 ${detail.applied_date}`, `Applied ${detail.applied_date}`)}</span>}
                  </div>
                  <div className="mt-3 flex flex-wrap items-center gap-2">
                    <div className="relative z-20 shrink-0">
                      <button
                        type="button"
                        onClick={() => { setPriorityMenuOpen((open) => !open); setStageMenuOpen(false); }}
                        disabled={prioritySaving || detailRefreshBlocked || stageMovePending.has(detail.id)}
                        aria-haspopup="true"
                        aria-expanded={priorityMenuOpen}
                        aria-label={l(`优先级：${detail.priority ? PRIORITY_META[detail.priority].label : "暂不设置"}，点击修改`, `Priority: ${detail.priority ?? "not set"}. Click to change`)}
                        className={`tag h-7 justify-center disabled:cursor-not-allowed disabled:opacity-50 ${
                          detail.priority ? PRIORITY_META[detail.priority].badgeClass : "bg-panel-2 text-ink-3"
                        }`}
                      >
                        {prioritySaving ? l("保存中", "Saving") : detail.priority ? l(PRIORITY_META[detail.priority].label, detail.priority === "high" ? "High" : detail.priority === "medium" ? "Medium" : "Low") : l("优先级", "Priority")}
                        <svg viewBox="0 0 16 16" className="h-2.5 w-2.5 opacity-70" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="m4 6 4 4 4-4" /></svg>
                      </button>
                      {priorityMenuOpen && (
                        <div className="absolute left-0 top-full z-30 mt-1 w-36 rounded-xl border border-line bg-panel py-1" style={{ boxShadow: "var(--shadow-pop)" }}>
                          <p className="px-3 pb-1.5 pt-2 text-[11px] font-medium text-ink-3">{l("设置优先级", "Set priority")}</p>
                          {(["high", "medium", "low", null] as const).map((priority) => (
                            <button
                              key={priority ?? "none"}
                              type="button"
                              onClick={() => void changePriority(priority)}
                              aria-pressed={detail.priority === priority}
                              className={`flex w-full items-center gap-2 px-3 py-2 text-left text-sm hover:bg-panel-2 ${detail.priority === priority ? "font-semibold text-ink" : "text-ink-2"}`}
                            >
                              <span className={`h-2 w-2 rounded-full ${priority ? PRIORITY_META[priority].dotClass : "border border-line-strong bg-panel"}`} />
                              <span>{priority ? l(PRIORITY_META[priority].label, priority === "high" ? "High" : priority === "medium" ? "Medium" : "Low") : l("暂不设置", "Not set")}</span>
                              {detail.priority === priority && <span aria-hidden="true" className="ml-auto text-accent">✓</span>}
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                    <div className="relative z-20 shrink-0">
                      <button
                        type="button"
                        onClick={() => { setStageMenuOpen((v) => !v); setPriorityMenuOpen(false); }}
                        disabled={detailEditing || prioritySaving || detailRefreshBlocked || stageMovePending.has(detail.id)}
                        aria-haspopup="true"
                        aria-expanded={stageMenuOpen}
                        aria-label={l(`当前阶段 ${stageLabel(detail.stage)}，点击修改`, `Current stage: ${stageLabel(detail.stage, locale)}. Click to change`)}
                        className={`tag h-7 ${STAGE_STYLES[detail.stage] ?? "bg-panel-2 text-ink-2"} ${detailEditing ? "opacity-60" : "hover:opacity-90"}`}
                      >
                        {stageLabel(detail.stage, locale)}
                        <svg viewBox="0 0 16 16" className="h-2.5 w-2.5 opacity-70" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round"><path d="m4 6 4 4 4-4" /></svg>
                      </button>
                      {stageMenuOpen && (
                        <div className="absolute right-0 top-full z-30 mt-1 w-32 rounded-xl border border-line bg-panel py-1" style={{ boxShadow: "var(--shadow-pop)" }}>
                          {columns.map(([key, label]) => (
                            <button
                              key={key}
                              type="button"
                              onClick={() => changeDetailStage(key)}
                              className={`flex w-full items-center gap-2 px-3 py-1.5 text-left text-sm hover:bg-panel-2 ${key === detail.stage ? "text-ink-3" : "text-ink"}`}
                            >
                              <span className={`h-1.5 w-1.5 rounded-full ${STAGE_DOT[key] ?? "bg-ink-3"}`} />
                              {label}
                            </button>
                          ))}
                        </div>
                      )}
                    </div>
                    {currentStepEditing ? (
                      <input
                        ref={currentStepInputRef}
                        value={currentStepDraft}
                        disabled={currentStepSaving}
                        aria-label={l("当前环节", "Current step")}
                        onChange={(event) => setCurrentStepDraft(limitCodePoints(event.target.value, 300))}
                        onBlur={() => void saveCurrentStep()}
                        onKeyDown={(event) => {
                          if (event.key === "Enter") event.currentTarget.blur();
                          if (event.key === "Escape") {
                            event.preventDefault();
                            event.stopPropagation();
                            setCurrentStepDraft(detail.current_step ?? "");
                            setCurrentStepEditing(false);
                          }
                        }}
                        className="input h-7 w-36 shrink-0 !rounded-lg !px-2 !py-0 text-xs sm:w-44"
                      />
                    ) : (
                      <button
                        type="button"
                        onClick={editCurrentStep}
                        disabled={detailRefreshBlocked || stageMovePending.has(detail.id)}
                        aria-label={l(`当前环节：${detail.current_step || "尚未记录"}，点击修改`, `Current step: ${detail.current_step || "not recorded"}. Click to change`)}
                        className="tag h-7 shrink-0 bg-panel-2 text-ink-2 transition-colors hover:bg-panel-3 disabled:cursor-not-allowed disabled:opacity-50"
                      >
                        {currentStepSaving ? l("保存中…", "Saving…") : detail.current_step || l("环节未记录", "Step not recorded")}
                      </button>
                    )}
                    {detailEditing ? (
                      <div className="ml-auto flex shrink-0 items-center gap-1.5">
                        <button type="button" onClick={cancelDetailEdit} disabled={profileSaving || noteSaving || historySaving} className="btn btn-sm">{l("取消", "Cancel")}</button>
                        <button
                          type="button"
                          onClick={() => void saveDetailEdit()}
                          disabled={profileSaving || noteSaving || historySaving}
                          className="btn-primary btn-sm"
                        >
                          {profileSaving || noteSaving ? l("保存中…", "Saving…") : l("保存更改", "Save changes")}
                        </button>
                      </div>
                    ) : null}
                  </div>
                  {pollMessage && <p role="status" className="mt-2 text-xs text-warn">{pollMessage}</p>}
                  {detail.prep_status === "failed" && (
                    <p className="mt-2 text-xs text-bad">
                      {l("上次调研生成失败。请到“公司调研”页重试。", "The last research run failed. Retry on the Research tab.")}
                    </p>
                  )}
                  <div className="mt-4 flex items-end gap-3">
                    <div role="tablist" aria-label={l("岗位详情视图", "Role detail views")} className="flex gap-5 text-sm">
                      {([["overview", l("概览", "Overview")], ["research", l("公司调研", "Research")], ["adaptation", l("简历优化", "Resume adaptation")]] as const).map(([tab, label]) => {
                      const active = detailTab === tab;
                      const dot = tab === "research"
                        ? (detail.prep_status === "ready" ? "bg-ok" : prepRunning ? "bg-info animate-pulse" : detail.prep_status === "failed" ? "bg-bad" : null)
                        : tab === "adaptation"
                          ? adaptationStatus === "running"
                            ? "bg-info animate-pulse"
                            : adaptationStatus === "ready" || Boolean(detail.prep?.resume_adaptation)
                              ? "bg-ok"
                              : null
                          : null;
                      return (
                        <button
                          key={tab}
                          type="button"
                          role="tab"
                          id={`detail-tab-${tab}`}
                          aria-controls="detail-tabpanel"
                          onClick={() => selectDetailTab(tab)}
                          disabled={detailEditing && tab !== "overview"}
                          aria-selected={active}
                          className={`relative flex items-center gap-1.5 pb-2.5 transition-colors ${
                            active ? "font-semibold text-ink" : "text-ink-3 hover:text-ink"
                          } ${detailEditing && tab !== "overview" ? "opacity-40" : ""}`}
                        >
                          {label}
                          {dot && <span className={`h-1.5 w-1.5 rounded-full ${dot}`} />}
                          {active && <span className="absolute inset-x-0 -bottom-px h-0.5 rounded-full bg-accent" />}
                        </button>
                      );
                      })}
                    </div>
                    {!detailEditing && (
                      <div className="ml-auto flex shrink-0 gap-1.5 pb-2">
                        <button type="button" onClick={startDetailEdit} disabled={detailRefreshBlocked} className="btn btn-sm">{l("编辑申请", "Edit application")}</button>
                        <button type="button" onClick={() => void prepareDetailDelete()} disabled={deletePreparing || detailRefreshBlocked} className="btn btn-sm text-bad hover:!bg-bad-soft">
                          {deletePreparing ? l("准备中…", "Preparing…") : l("删除申请", "Delete application")}
                        </button>
                      </div>
                    )}
                  </div>
                </div>
                {error && (
                  <div role="alert" aria-live="assertive" className="shrink-0 border-b border-bad/20 bg-bad-soft px-5 py-2.5 text-sm text-bad">
                    {error}
                  </div>
                )}
                <div
                  id="detail-tabpanel"
                  role="tabpanel"
                  aria-labelledby={`detail-tab-${detailTab}`}
                  className="flex-1 overflow-y-auto px-4 py-5 sm:px-6"
                >
                  {detailTab === "overview" && (
                    <div className="flex flex-col gap-4">

          <div
            className={`rounded-xl border transition-colors ${
              detailEditing || noteSaving
                ? "border-line-strong bg-panel p-4"
                : "border-line-2 bg-panel-2/50"
            }`}
          >
            {detailEditing ? (
              <>
                <div className="mb-2 flex items-center">
                  <p className="text-sm font-medium">{l("备注", "Notes")}</p>
                </div>
                <textarea
                  aria-label={l("岗位备注", "Role notes")}
                  value={noteDraft}
                  disabled={profileSaving || noteSaving || historySaving}
                  onChange={(event) => {
                    setNoteDraft(limitCodePointsWhileEditing(
                      noteDraft,
                      event.target.value,
                      2000,
                    ));
                    setNoteDirty(true);
                  }}
                  placeholder={l("例如：内推人、沟通重点、需要确认的问题……", "For example: referrer, discussion points, questions to confirm…")}
                  className="min-h-24 w-full resize-y bg-transparent p-0 text-sm text-ink outline-none placeholder:text-ink-3"
                />
                {noteConflict && (
                  <aside role="alert" className="mt-2 rounded-lg bg-warn-soft px-3 py-2 text-xs text-warn">
                    <p className="font-medium">{l("保存前，这条备注已被 Agent 或另一窗口更新。", "An agent or another window updated these notes before you saved.")}</p>
                    <p className="mt-1 text-ink-2">{l("当前已保存内容：", "Currently saved content:")}</p>
                    <p className="mt-1 max-h-28 overflow-y-auto whitespace-pre-wrap break-words rounded-md bg-panel px-2 py-1.5 text-ink-2">
                      {noteConflict.savedNote || l("（当前备注为空）", "(No saved notes)")}
                    </p>
                    <button
                      type="button"
                      onClick={() => {
                        const mergedDraft = mergeConflictingApplicationNote(
                          noteExpected,
                          noteDraft,
                          noteConflict.savedNote,
                        );
                        setNoteDraft(mergedDraft);
                        setNoteExpected(noteConflict.savedNote);
                        setNoteExpectedRevision(detail?.revision ?? null);
                        setNoteDirty(mergedDraft !== (noteConflict.savedNote?.trim() ?? ""));
                        setNoteConflict({ ...noteConflict, rebased: true });
                      }}
                      disabled={noteSaving || noteConflict.rebased}
                      className="btn btn-sm mt-2"
                    >
                      {noteConflict.rebased ? l("已合并，请检查草稿", "Merged—review the draft") : l("合并最新内容到我的草稿", "Merge latest content into my draft")}
                    </button>
                    <p className="mt-1">
                      {noteConflict.rebased
                        ? l("最新内容已加入草稿；请检查后再保存。", "The latest content is now in your draft. Review it before saving.")
                        : l("系统会保留两边内容；合并后请检查再保存。", "Both versions will be preserved. Review the merged draft before saving.")}
                    </p>
                  </aside>
                )}
                <div className="mt-2 text-right text-xs text-ink-3">
                  <span className={`tabular-nums ${codePointLength(noteDraft) > 2000 ? "text-bad" : ""}`}>
                    {codePointLength(noteDraft)} / 2000
                    {codePointLength(noteDraft) > 2000 ? l(" · 合并后过长，请整理再保存", " · Too long after merging; shorten it before saving") : ""}
                  </span>
                </div>
              </>
            ) : (
              <>
                <div className="flex items-center px-3.5 py-2.5">
                  <span className="text-sm font-medium">{l("备注", "Notes")}</span>
                </div>
                <p className={`whitespace-pre-wrap break-words px-3.5 pb-3 text-sm ${detail.application_note ? "text-ink-2" : "text-ink-3"}`}>
                  {detail.application_note || l("还没有备注；点击右上角“编辑”后记录。", "No notes yet. Choose Edit application to add them.")}
                </p>
              </>
            )}
          </div>

          <ReviewUndoOperationsPanel
            active
            refreshSignal={reviewUndoRefreshSignal}
            operationIds={reviewUndoOperationIds}
            className="mb-5"
            onOperationSettled={() => {
              setReviewUndoOperationIds([]);
              const applicationId = selectedIdRef.current;
              void refresh(applicationId ?? undefined, selectionEpochRef.current);
            }}
          />

          <ApplicationDeleteOperationsPanel
            active
            refreshSignal={deleteRefreshSignal}
            operationIds={deleteOperationIds}
            className="mb-5"
            onOperationSettled={() => {
              const applicationId = selectedIdRef.current;
              void refresh(applicationId ?? undefined, selectionEpochRef.current);
            }}
          />

          {!detailEditing && (detail.jd_text || (detail.jd_parsed?.skills?.length ?? 0) > 0) && (
            <div className="rounded-xl border border-line-2 bg-panel-2/50">
              <button
                type="button"
                onClick={() => setJdExpanded((v) => !v)}
                aria-expanded={jdExpanded}
                className="flex w-full items-center gap-2 px-3.5 py-2.5 text-left"
              >
                <span className="shrink-0 text-sm font-medium">{l("岗位描述", "Job description")}</span>
                {!jdExpanded && (detail.jd_parsed?.skills?.length ?? 0) > 0 && (
                  <span className="flex min-w-0 flex-1 gap-1 overflow-hidden">
                    {(detail.jd_parsed?.skills ?? []).slice(0, 4).map((skill) => (
                      <span key={skill} className="tag shrink-0 bg-panel text-ink-2">{skill}</span>
                    ))}
                    {(detail.jd_parsed?.skills?.length ?? 0) > 4 && (
                      <span className="tag shrink-0 bg-panel text-ink-3">+{(detail.jd_parsed?.skills?.length ?? 0) - 4}</span>
                    )}
                  </span>
                )}
                <svg viewBox="0 0 16 16" className={`ml-auto h-3.5 w-3.5 shrink-0 text-ink-3 transition-transform ${jdExpanded ? "rotate-180" : ""}`} fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="m4 6 4 4 4-4" /></svg>
              </button>
              {jdExpanded && (
                <div className="px-3.5 pb-3">
                  {(detail.jd_parsed?.skills?.length ?? 0) > 0 && (
                    <div className="flex flex-wrap gap-1.5">
                      {(detail.jd_parsed?.skills ?? []).map((skill) => (
                        <span key={skill} className="tag bg-panel text-ink-2">{skill}</span>
                      ))}
                    </div>
                  )}
                  {detail.jd_text && (
                    <p className="mt-3 max-h-72 overflow-y-auto whitespace-pre-wrap text-sm text-ink-2">{detail.jd_text}</p>
                  )}
                </div>
              )}
            </div>
          )}

          {detailEditing && profileDraft && (
            <fieldset
              disabled={profileSaving || noteSaving || historySaving}
              aria-labelledby="job-profile-editor-title"
              className="rounded-2xl border border-line bg-panel-2/55 p-4 disabled:opacity-70"
            >
              <div className="mb-3">
                <h3 id="job-profile-editor-title" className="text-sm font-semibold">{l("岗位信息", "Role information")}</h3>
                <p className="mt-0.5 text-xs text-ink-3">{l("修改名称或岗位描述后，旧的公司调研会自动失效。", "Changing the name or job description invalidates older company research.")}</p>
              </div>
              {profileConflict && (
                <aside
                  role={profileConflict.fields.length > 0 ? "alert" : "status"}
                  className={`mb-3 rounded-xl px-3 py-2.5 text-xs ${
                    profileConflict.fields.length > 0
                      ? "bg-warn-soft text-warn"
                      : "bg-info-soft text-info"
                  }`}
                >
                  {profileConflict.fields.length > 0 ? (
                    <>
                      <p className="font-medium">
                        {l("另一窗口也修改了：", "Another window also changed: ")}{profileConflict.fields.map(
                          (field) => (locale === "en"
                            ? PROFILE_FIELD_LABELS_EN
                            : PROFILE_FIELD_LABELS_ZH)[field],
                        ).join(l("、", ", "))}{l("。你的版本已保留。", ". Your version has been preserved.")}
                      </p>
                      <p className="mt-1">{l("请检查标出的字段；确认后才可再次保存。", "Review the highlighted fields and confirm before saving again.")}</p>
                      {!profileConflict.acknowledged && (
                        <button
                          type="button"
                          onClick={() => setProfileConflict({ ...profileConflict, acknowledged: true })}
                          className="btn btn-sm mt-2"
                        >
                          {l("我已检查，继续保存", "I reviewed them—continue")}
                        </button>
                      )}
                    </>
                  ) : (
                    <p>{l("已合并另一窗口的最新修改；你的草稿字段保持不变。", "The other window's latest changes were merged; your draft fields are unchanged.")}</p>
                  )}
                </aside>
              )}
              <div className="grid gap-3 md:grid-cols-2">
                {([
                  [l("公司名称", "Company"), "company", 200],
                  [l("岗位名称", "Role"), "position", 300],
                  [l("部门（可选）", "Department (optional)"), "department", 200],
                  [l("渠道（可选）", "Channel (optional)"), "channel", 100],
                ] as const).map(([label, field, maxLength]) => (
                  <label key={field} className="grid gap-1.5 text-xs font-medium text-ink-2">
                    {label}
                    <input
                      value={profileDraft[field]}
                      onChange={(event) => {
                        setProfileDraft({
                          ...profileDraft,
                          [field]: limitCodePoints(event.target.value, maxLength),
                        });
                        setProfileDirty(true);
                      }}
                      className={`input ${profileConflict?.fields.includes(field) ? "!border-warn" : ""}`}
                    />
                  </label>
                ))}
                <label className="grid gap-1.5 text-xs font-medium text-ink-2">
                  {l("投递日期（可选）", "Application date (optional)")}
                  <input
                    type="date"
                    value={profileDraft.applied_date}
                    onChange={(event) => {
                      setProfileDraft({ ...profileDraft, applied_date: event.target.value });
                      setProfileDirty(true);
                    }}
                    className={`input ${profileConflict?.fields.includes("applied_date") ? "!border-warn" : ""}`}
                  />
                </label>
                {profileDraft.stage === "pooled" && (
                  <label className="grid gap-1.5 text-xs font-medium text-ink-2 md:col-span-2">
                    {l("泡池原因（可选）", "Reason for pooling (optional)")}
                    <textarea
                      value={profileDraft.pause_reason}
                      onChange={(event) => {
                        setProfileDraft({
                          ...profileDraft,
                          pause_reason: limitCodePoints(event.target.value, 1_000),
                        });
                        setProfileDirty(true);
                      }}
                      placeholder={l("例如：等待 HC、招聘暂缓；离开泡池阶段后不会保存此字段", "For example: awaiting headcount or hiring paused. This field is cleared when leaving Pooled.")}
                      className={`input min-h-16 resize-y ${profileConflict?.fields.includes("pause_reason") ? "!border-warn" : ""}`}
                    />
                  </label>
                )}
              </div>
              <label className="mt-3 grid gap-1.5 text-xs font-medium text-ink-2">
                {l("岗位描述（可选）", "Job description (optional)")}
                <textarea
                  value={profileDraft.jd_text}
                  onChange={(event) => {
                    setProfileDraft({
                      ...profileDraft,
                      jd_text: limitCodePoints(event.target.value, 50_000),
                    });
                    setProfileDirty(true);
                  }}
                  placeholder={l("建议直接复制粘贴招聘页面中的完整岗位信息，包括职责、要求和加分项", "Paste the complete listing, including responsibilities, requirements, and preferred qualifications.")}
                  className={`input min-h-28 resize-y ${profileConflict?.fields.includes("jd_text") ? "!border-warn" : ""}`}
                />
              </label>
            </fieldset>
          )}

          <section aria-labelledby="next-action-title" className="rounded-xl border border-line-2 bg-panel-2/50 p-3.5">
            <div className="flex items-center justify-between gap-3">
              <div>
                <p id="next-action-title" className="section-label">{l("下一步安排", "Next action")}</p>
                {detail.stage === "pooled" && detail.next_action && (
                  <p className="mt-1 text-xs text-ember">{l("安排已保留；泡池期间不会显示在近期日程中。", "The action is preserved but hidden from the upcoming agenda while this role is pooled.")}</p>
                )}
              </div>
            </div>
            {detailEditing && nextActionDraft && !stageEndsApplication(profileDraft?.stage ?? detail.stage) ? (
              <fieldset disabled={profileSaving || noteSaving || historySaving} className="mt-3 grid gap-3 disabled:opacity-70">
                {nextActionDraft.conflicted && (
                  <div role="alert" className="rounded-lg border border-warn/30 bg-warn-soft p-3 text-sm">
                    <p className="font-semibold text-warn">{l("下一步已在 Agent 或另一窗口中变化", "The next action changed in an agent or another window")}</p>
                    <p className="mt-1 text-xs text-ink-2">{l("你的整页草稿仍保留，但不会覆盖最新安排。请先切换到最新安排，再保存整页。", "Your page draft is preserved but will not overwrite the latest action. Switch to the latest action before saving the page.")}</p>
                    <div className="mt-2 flex justify-end">
                      <button type="button" onClick={() => setNextActionDraft(nextActionDraftFromDetail(detail))} className="btn btn-sm">{l("编辑最新安排", "Edit latest action")}</button>
                    </div>
                  </div>
                )}
                <div className="grid gap-3 sm:grid-cols-2">
                  <label className="text-xs text-ink-3">
                    {l("完成后阶段（可不变）", "Stage after completion (may stay the same)")}
                    <select
                      aria-label={l("下一阶段", "Next stage")}
                      value={nextActionDraft.stage}
                      onChange={(event) => setNextActionDraft({
                        ...nextActionDraft,
                        stage: event.target.value as ApplicationStage,
                      })}
                      className="input mt-1 w-full"
                    >
                      {columns.map(([stage, label]) => <option key={stage} value={stage}>{label}</option>)}
                    </select>
                  </label>
                  <label className="text-xs text-ink-3">
                    {l("下一步（可选）", "Next action (optional)")}
                    <input
                      aria-label={l("下一步", "Next action")}
                      value={nextActionDraft.step}
                      onChange={(event) => setNextActionDraft({
                        ...nextActionDraft,
                        step: limitCodePoints(event.target.value, 300),
                      })}
                      placeholder={l("例如：二面、在线测评、等待结果", "For example: second interview, assessment, await decision")}
                      className="input mt-1 w-full"
                    />
                  </label>
                  <label className="text-xs text-ink-3">
                    {l("日期（可选）", "Date (optional)")}
                    <input type="date" aria-label={l("下一步日期", "Next-action date")} value={nextActionDraft.date} onChange={(event) => setNextActionDraft({ ...nextActionDraft, date: event.target.value, time: event.target.value ? nextActionDraft.time : "" })} className="input mt-1 w-full" />
                  </label>
                  <label className="text-xs text-ink-3">
                    {l("时间（可选）", "Time (optional)")}
                    <input type="time" aria-label={l("下一步时间", "Next-action time")} value={nextActionDraft.time} disabled={!nextActionDraft.date} onChange={(event) => setNextActionDraft({ ...nextActionDraft, time: event.target.value })} className="input mt-1 w-full" />
                  </label>
                </div>
                <label className="text-xs text-ink-3">
                  {l("说明（可选）", "Notes (optional)")}
                  <textarea aria-label={l("下一步说明", "Next-action notes")} value={nextActionDraft.note} onChange={(event) => setNextActionDraft({ ...nextActionDraft, note: limitCodePoints(event.target.value, 2000) })} placeholder={l("准备重点、会议方式、联系人等", "Preparation priorities, meeting format, contact, and so on")} className="input mt-1 min-h-20 w-full resize-y" />
                </label>
                <div className="flex items-center justify-between gap-3">
                  <p className="text-xs text-ink-3">{l("填写名称即可创建安排；与其他字段一起由右上角“保存更改”统一保存。", "Enter a name to create the action. Save it with the other fields using Save changes.")}</p>
                  {(nextActionDraft.base_next_action !== null || nextActionDraft.step || nextActionDraft.date || nextActionDraft.time || nextActionDraft.note) && (
                    <button
                      type="button"
                      onClick={() => setNextActionDraft({
                        ...nextActionDraft,
                        conflicted: false,
                        stage: profileDraft?.stage ?? detail.stage,
                        step: "",
                        date: "",
                        time: "",
                        note: "",
                      })}
                      className="btn btn-sm shrink-0"
                    >
                      {l("清除安排", "Clear action")}
                    </button>
                  )}
                </div>
              </fieldset>
            ) : detailEditing ? (
              <p className="mt-2 text-sm text-ink-3">
                {l("当前编辑后的阶段会结束流程，保存时将清空下一步安排。", "The edited stage ends this process, so saving will clear the next action.")}
              </p>
            ) : completionDraft && (completionDraft.conflicted || !detail.next_action) ? (
              <div role="alert" className="mt-3 rounded-lg border border-warn/30 bg-warn-soft p-3 text-sm">
                <p className="font-semibold text-warn">{l("这项安排已在 Agent 或另一窗口中变更", "This action changed in an agent or another window")}</p>
                <p className="mt-1 text-xs text-ink-2">{l("本次完成草稿未写入，你填写的日期、结果和说明仍保留。要完成的安排已变化或不存在，请先放弃草稿，再根据最新状态重新记录。", "Your completion was not written, but its date, outcome, and notes remain in the draft. Discard it, then record completion against the latest action.")}</p>
                {(completionDraft.occurred_date || completionDraft.outcome || completionDraft.summary.trim()) && (
                  <p className="mt-2 rounded-md bg-panel/70 px-2.5 py-2 text-xs text-ink-2">
                    {[completionDraft.occurred_date, completionDraft.outcome
                      ? outcomeLabel(completionDraft.outcome, locale)
                      : null, completionDraft.summary.trim() || null].filter(Boolean).join(" · ")}
                  </p>
                )}
                <div className="mt-3 flex justify-end">
                  <button type="button" onClick={() => setCompletionDraft(null)} className="btn btn-sm">{l("放弃这份草稿", "Discard this draft")}</button>
                </div>
              </div>
            ) : detail.next_action ? (
              <div className="mt-3">
                <div className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                  <p className="text-sm font-semibold">{detail.next_action.step}</p>
                  {detail.next_action.stage !== detail.stage && (
                    <span className="tag bg-info-soft text-info">{l("完成后进入", "Then move to ")}{stageLabel(detail.next_action.stage, locale)}</span>
                  )}
                </div>
                {(detail.next_action.date || detail.next_action.time) && (
                  <p className="mt-1 text-sm tabular-nums text-ink-2">
                    {[detail.next_action.date, detail.next_action.time].filter(Boolean).join(" ")}
                  </p>
                )}
                {detail.next_action.note && <p className="mt-1 whitespace-pre-wrap text-sm text-ink-2">{detail.next_action.note}</p>}
                {completionDraft ? (
                  <fieldset disabled={nextActionSaving} className="mt-3 rounded-lg border border-line bg-panel p-3 disabled:opacity-70">
                    <p className="text-xs font-semibold">{l(`确认完成「${detail.next_action.step}」`, `Confirm completion of “${detail.next_action.step}”`)}</p>
                    <p className="mt-1 text-xs text-ink-3">
                      {l("完成后：", "After completion: ")}{stageLabel(completionTargetStage ?? detail.next_action.stage, locale)} · {l(`当前环节「${detail.next_action.step}」。`, `current step “${detail.next_action.step}”. `)}
                      {completionTargetStage === detail.stage ? l("阶段不变，只推进具体环节。", "The stage stays the same; only the step advances.") : l("阶段将同步更新。", "The stage will update too.")}
                    </p>
                    <div className="mt-2 grid gap-2 sm:grid-cols-2">
                      <label className="text-xs text-ink-3">{l("完成日期", "Completion date")}<input type="date" aria-label={l("下一步完成日期", "Next-action completion date")} value={completionDraft.occurred_date} onChange={(event) => setCompletionDraft({ ...completionDraft, occurred_date: event.target.value })} className="input mt-1 w-full" /></label>
                      <label className="text-xs text-ink-3">{l("结果（可选）", "Outcome (optional)")}<select aria-label={l("下一步完成结果", "Next-action outcome")} value={completionDraft.outcome} onChange={(event) => changeCompletionOutcome(event.target.value as CompleteNextActionDraft["outcome"])} className="input mt-1 w-full"><option value="">{l("未设置", "Not set")}</option><option value="passed">{l("通过", "Passed")}</option><option value="failed">{l("未通过", "Not passed")}</option><option value="cancelled">{l("取消", "Cancelled")}</option></select></label>
                      <label className="text-xs text-ink-3 sm:col-span-2">{l("说明（可选）", "Notes (optional)")}<textarea aria-label={l("下一步完成说明", "Completion notes")} value={completionDraft.summary} onChange={(event) => setCompletionDraft({ ...completionDraft, summary: limitCodePoints(event.target.value, 2000) })} className="input mt-1 min-h-16 w-full resize-y" /></label>
                    </div>
                    {completionClosesApplication ? (
                      <p className="mt-3 text-xs text-ink-3">{l("完成后将进入", "Completion moves this role to ")}{stageLabel(completionTargetStage ?? detail.next_action.stage, locale)}{l("，现有安排会清空，不再创建新的下一步。", ". The existing action is cleared and no new one is created.")}</p>
                    ) : (
                      <label className="mt-3 flex cursor-pointer items-center gap-2 text-xs text-ink-2"><input type="checkbox" checked={completionDraft.set_next_action} onChange={(event) => setCompletionDraft({ ...completionDraft, set_next_action: event.target.checked })} />{l("继续安排新的下一步", "Schedule another next action")}</label>
                    )}
                    {!completionClosesApplication && completionDraft.set_next_action && (
                      <div className="mt-2 grid gap-2 sm:grid-cols-2">
                        <label className="text-xs text-ink-3">{l("完成后阶段（可不变）", "Stage after completion (may stay the same)")}<select aria-label={l("新下一步完成后阶段", "Stage after the new next action")} value={completionDraft.next_action.stage} onChange={(event) => setCompletionDraft({ ...completionDraft, next_action: { ...completionDraft.next_action, stage: event.target.value as ApplicationStage } })} className="input mt-1 w-full">{columns.map(([stage, label]) => <option key={stage} value={stage}>{label}</option>)}</select></label>
                        <label className="text-xs text-ink-3">{l("下一步", "Next action")} <span className="text-bad">*</span><input aria-label={l("完成后的下一步", "Next action after completion")} value={completionDraft.next_action.step} onChange={(event) => setCompletionDraft({ ...completionDraft, next_action: { ...completionDraft.next_action, step: limitCodePoints(event.target.value, 300) } })} className="input mt-1 w-full" /></label>
                        <label className="text-xs text-ink-3">{l("日期（可选）", "Date (optional)")}<input type="date" aria-label={l("完成后的下一步日期", "Date of next action after completion")} value={completionDraft.next_action.date} onChange={(event) => setCompletionDraft({ ...completionDraft, next_action: { ...completionDraft.next_action, date: event.target.value, time: event.target.value ? completionDraft.next_action.time : "" } })} className="input mt-1 w-full" /></label>
                        <label className="text-xs text-ink-3">{l("时间（可选）", "Time (optional)")}<input type="time" aria-label={l("完成后的下一步时间", "Time of next action after completion")} value={completionDraft.next_action.time} disabled={!completionDraft.next_action.date} onChange={(event) => setCompletionDraft({ ...completionDraft, next_action: { ...completionDraft.next_action, time: event.target.value } })} className="input mt-1 w-full" /></label>
                        <label className="text-xs text-ink-3 sm:col-span-2">{l("说明（可选）", "Notes (optional)")}<textarea aria-label={l("完成后的下一步说明", "Notes for next action after completion")} value={completionDraft.next_action.note} onChange={(event) => setCompletionDraft({ ...completionDraft, next_action: { ...completionDraft.next_action, note: limitCodePoints(event.target.value, 2000) } })} className="input mt-1 min-h-16 w-full resize-y" /></label>
                      </div>
                    )}
                    <div className="mt-3 flex justify-end gap-2">
                      <button type="button" onClick={() => setCompletionDraft(null)} disabled={nextActionSaving} className="btn btn-sm">{l("取消", "Cancel")}</button>
                      <button type="button" onClick={() => void completeNextAction()} disabled={nextActionSaving || (!completionClosesApplication && completionDraft.set_next_action && !completionDraft.next_action.step.trim())} className="btn-primary btn-sm">{nextActionSaving ? l("处理中…", "Processing…") : l("确认完成", "Confirm completion")}</button>
                    </div>
                  </fieldset>
                ) : (
                  <div className="mt-3 flex flex-wrap gap-2">
                    <button type="button" onClick={startCompletingNextAction} disabled={nextActionSaving} className="btn-primary btn-sm">{l("完成下一步", "Complete next action")}</button>
                  </div>
                )}
              </div>
            ) : (
              <p className="mt-2 text-sm text-ink-3">
                {detail.stage === "rejected" || detail.stage === "withdrawn"
                  ? l("流程已结束，不再保留下一步安排。", "This process has ended, so no next action is retained.")
                  : l("还没有下一步；点击右上角“编辑”后填写，保存后会进入近期日程。", "No next action yet. Choose Edit application to add one to the upcoming agenda.")}
              </p>
            )}
          </section>

          <div className="flex items-center justify-between gap-2">
            <p className="section-label">{l("历程", "History")}</p>
            {detailEditing && (
              <button
                type="button"
                onClick={startCreatingHistoryEntry}
                disabled={historySaving || historyDraft !== null}
                className="btn btn-sm"
              >
                {l("添加历程", "Add history entry")}
              </button>
            )}
          </div>
          {historyDraft?.entryId === null && (
            <HistoryEntryForm
              draft={historyDraft}
              saving={historySaving}
              currentStage={detail.stage}
              allowProgressUpdate
              onChange={setHistoryDraft}
              onResolveConflict={() => {
                setHistoryDraft((current) => current
                  ? confirmHistoryDraftAgainstLatest(current)
                  : current);
                setError(l("已基于最新岗位状态确认；请再次保存这条历程。", "Confirmed against the latest role state. Save this history entry again."));
              }}
              onCancel={() => setHistoryDraft(null)}
              onSave={() => void saveHistoryEntry()}
            />
          )}
          {detail.timeline_entries.length === 0 ? (
            <p className="text-sm text-ink-3">{l("暂无历程", "No history yet")}</p>
          ) : (
            <ol className="flex flex-col">
              {[...detail.timeline_entries].reverse().map((entry, reverseIndex, ordered) => {
                const isLast = reverseIndex === ordered.length - 1;
                const displaySummary = timelineEntrySummary(entry, locale);
                const displayTitle = timelineEntryTitle(entry, locale);
                return (
                  <li key={entry.id} className="flex gap-3">
                    <div className="flex flex-col items-center">
                      <span className={`mt-1.5 h-2.5 w-2.5 shrink-0 rounded-full border-2 border-panel ${timelineEntryNodeClass(entry)}`} />
                      {!isLast && <span className="w-px flex-1 bg-line" />}
                    </div>
                    <div className="min-w-0 flex-1 pb-4">
                      {detailEditing && historyEditDrafts[entry.id] ? (
                        <>
                          <HistoryEntryForm
                            draft={historyEditDrafts[entry.id]}
                            saving={historySaving || reviewUndoPreparingId === entry.id}
                            currentStage={entry.to_stage}
                            caption={`${timelineEntryDateLabel(entry, today, locale)} · ${timelineEntryTitle(entry, locale)}`}
                            changed={historyDraftChanged(historyEditDrafts[entry.id], entry)}
                            onChange={(draft) => setHistoryEditDrafts((current) => ({
                              ...current,
                              [entry.id]: draft,
                            }))}
                            onResolveConflict={() => {
                              setHistoryEditDrafts((current) => {
                                const currentDraft = current[entry.id];
                                if (!currentDraft) return current;
                                return {
                                  ...current,
                                  [entry.id]: {
                                    ...confirmHistoryDraftAgainstLatest(currentDraft),
                                    entry_conflict: null,
                                  },
                                };
                              });
                              setError("已确认使用你的草稿覆盖冲突字段；请再次保存整页修改。");
                            }}
                            onDiscardConflict={() => {
                              setHistoryEditDrafts((current) => ({
                                ...current,
                                [entry.id]: historyDraftFromEntry(entry, detail.revision),
                              }));
                              setError("已放弃这条历程草稿，并采用另一窗口保存的最新值。");
                            }}
                            onDelete={entry.source === "review"
                              ? () => void prepareReviewEntryUndo(entry)
                              : () => requestHistoryEntryDelete(entry)}
                            deleteLabel={entry.source === "review"
                              ? l("撤销整次复盘", "Undo the entire review")
                              : l("删除这条历程", "Delete this history entry")}
                          />
                          {entry.source === "review" && (
                            <p className="-mt-2 mb-4 text-xs text-ink-3">
                              {l("这条历程来自复盘，可修改事实；若整次复盘记错，请使用上方按钮核对完整影响后撤销。", "This entry came from a review. You can edit its facts; if the whole review is wrong, use the button above to inspect the full impact before undoing it.")}
                            </p>
                          )}
                        </>
                      ) : (
                        <>
                          <div className="flex items-start gap-2">
                            <span className="text-sm font-semibold">{displayTitle}</span>
                            {entry.outcome && (
                              <span className={`tag ${entry.outcome === "passed" ? "bg-ok-soft text-ok" : entry.outcome === "failed" ? "bg-bad-soft text-bad" : "bg-panel-2 text-ink-2"}`}>
                                {outcomeLabel(entry.outcome, locale)}
                              </span>
                            )}
                            <span className="ml-auto shrink-0 text-xs tabular-nums text-ink-3">{timelineEntryDateLabel(entry, today, locale)}</span>
                          </div>
                          {displaySummary && displaySummary.trim() !== displayTitle && (
                            <p className="mt-0.5 whitespace-pre-wrap text-sm text-ink-2">{displaySummary}</p>
                          )}
                          {(entry.from_stage !== entry.to_stage || entry.from_step !== entry.to_step) && (
                            <div className="mt-1 flex flex-wrap items-center gap-1 text-xs text-ink-3">
                              <span className="rounded-md bg-panel-2 px-2 py-1">
                                {stageLabel(entry.from_stage, locale)}{entry.from_step ? ` · ${entry.from_step}` : ""}
                              </span>
                              <span aria-hidden="true">→</span>
                              <span className="rounded-md bg-panel-2 px-2 py-1">
                                {stageLabel(entry.to_stage, locale)}{entry.to_step ? ` · ${entry.to_step}` : ""}
                              </span>
                            </div>
                          )}
                        </>
                      )}
                      {pendingHistoryDelete?.id === entry.id && (
                        <section
                          role="alertdialog"
                          aria-labelledby={`history-delete-title-${entry.id}`}
                          aria-describedby={`history-delete-description-${entry.id}`}
                          className="mt-2 rounded-xl border border-bad/30 bg-bad-soft p-3 text-sm"
                        >
                          <p id={`history-delete-title-${entry.id}`} className="font-semibold text-bad">
                            {l("删除这条历程？", "Delete this history entry?")}
                          </p>
                          <p id={`history-delete-description-${entry.id}`} className="mt-1 text-ink-2">
                            {l("若这条记录决定了当前阶段或环节，删除后可能回退当前状态。独立设置的下一步会保留；仅在回退到“不再跟进”或“已挂”时清除不兼容安排。若已有更新，系统会阻止覆盖。", "If this entry determines the current stage or step, deleting it may roll that state back. Independently scheduled next actions remain unless the rollback closes the process. Newer updates are protected from being overwritten.")}
                          </p>
                          <p className="mt-2 rounded-lg bg-panel/70 px-2.5 py-2 text-xs text-ink-2">
                            {timelineEntryDateLabel(entry, today, locale)} · {displayTitle}
                            {displaySummary ? ` · ${displaySummary}` : ""}
                          </p>
                          <label className="mt-2 flex cursor-pointer items-start gap-2 text-xs text-ink-2">
                            <input
                              type="checkbox"
                              checked={rememberHistoryDeleteChoice}
                              onChange={(changeEvent) => setRememberHistoryDeleteChoice(
                                changeEvent.target.checked,
                              )}
                              className="mt-0.5"
                            />
                            <span>{l("以后删除历程时不再询问", "Don't ask again when deleting history entries")}</span>
                          </label>
                          <div className="mt-3 flex justify-end gap-2">
                            <button
                              ref={historyDeleteCancelRef}
                              type="button"
                              onClick={() => {
                                setPendingHistoryDelete(null);
                                setRememberHistoryDeleteChoice(false);
                              }}
                              disabled={historySaving}
                              className="btn btn-sm"
                            >
                              {l("取消", "Cancel")}
                            </button>
                            <button
                              type="button"
                              onClick={confirmHistoryEntryDelete}
                              disabled={historySaving}
                              className="btn btn-sm btn-danger"
                            >
                              {historySaving ? l("删除中…", "Deleting…") : l("确认删除", "Confirm deletion")}
                            </button>
                          </div>
                        </section>
                      )}
                    </div>
                  </li>
                );
              })}
            </ol>
          )}
                    </div>
                  )}

                  {detailTab === "research" && (
                    briefing ? (
                      <div className="flex flex-col gap-3">
                        <div className="flex items-center justify-between gap-2">
                          <p className="section-label">{l("公司调研", "Company research")}</p>
                          <button
                            onClick={() => void generatePrep(detail.id, { forceTakeover: pollTimedOut, refreshResearch: true })}
                            disabled={prepRunning && !pollTimedOut}
                            title={l("重跑考点与建议答案，并按当前联网权限决定是否刷新公司调研（会调模型）", "Regenerate interview focus areas and suggested answers, refreshing company research if current online permissions allow. This invokes the model.")}
                            className="btn-primary btn-sm"
                          >
                            {prepRunning && !pollTimedOut ? l("生成中…", "Generating…") : pollTimedOut ? l("安全接管并重试", "Take over safely and retry") : l("重新生成", "Regenerate")}
                          </button>
                        </div>
                        {prepRunning && (
                          <p className="text-xs text-info">{l("调研还在生成中，报告与建议答案完成后会自动补全（通常一到三分钟，最长八分钟）。", "Research is still running. Reports and suggested answers will appear automatically—usually in 1–3 minutes, with an 8-minute maximum.")}</p>
                        )}
                        <div className="rounded-2xl border border-line bg-panel-2 p-4">
                          <Markdown text={briefing} />
                        </div>
                      </div>
                    ) : briefingLoading ? (
                      <p className="p-6 text-center text-sm text-ink-3">{l("正在打开公司调研…", "Opening company research…")}</p>
                    ) : (
                      <div className="flex flex-col items-center gap-2.5 px-6 py-10 text-center">
                        <svg viewBox="0 0 24 24" className="h-6 w-6 text-ink-3" fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round"><path d="M4 5.5A1.5 1.5 0 0 1 5.5 4h9L19 8.5V18.5a1.5 1.5 0 0 1-1.5 1.5h-12A1.5 1.5 0 0 1 4 18.5Z" /><path d="M14.5 4v4.5H19M8 12h8M8 15.5h5" /></svg>
                        <p className="text-sm font-semibold">{l("还没有这家公司的调研", "No research for this company yet")}</p>
                        <p className="max-w-sm text-xs text-ink-3">{l("考前一页纸：公司近况、业务与岗位考点、建议答案。联网检索按你的设置执行，通常 1–3 分钟。", "A concise brief covering company updates, business and role focus areas, and suggested answers. Online research follows your settings and usually takes 1–3 minutes.")}</p>
                        <button onClick={() => void openBriefing()} disabled={briefingLoading} className="btn-primary mt-1">{l("生成公司调研", "Generate company research")}</button>
                      </div>
                    )
                  )}

                  {detailTab === "adaptation" && (
                    <ResumeAdaptationPanel
                      applicationId={detail.id}
                      editRevision={detail.revision}
                      onApplicationChanged={async () => {
                        const loaded = await loadDetail(
                          detail.id,
                          selectionEpochRef.current,
                          undefined,
                          true,
                        );
                        if (!loaded) throw new Error(l(
                          "岗位详情刷新失败，请重新打开后重试",
                          "Could not refresh role details. Reopen the role and try again.",
                        ));
                      }}
                      onEditJd={startDetailEdit}
                      onResearchAction={async (action: Exclude<ResumeAdaptationResearchAction, null>) => {
                        const started = await generatePrep(detail.id, {
                          forceTakeover: pollTimedOut,
                          refreshResearch: action === "refresh" || action === "restart",
                        });
                        if (!started) throw new Error(l(
                          "公司调研未能启动，请检查上方提示",
                          "Company research did not start. Check the notice above.",
                        ));
                      }}
                      onConfigureResearch={() => { window.location.hash = "#/settings"; }}
                      onConfigureModel={() => { window.location.hash = "#/settings"; }}
                      onStatusChange={setAdaptationStatus}
                    />
                  )}
                </div>
              </>
            )}
          </aside>
        </div>
      )}
      {selectedId === null && error && <p role="alert" className="text-sm text-bad">{error}</p>}
    </div>
  );
}

function HistoryEntryForm({
  draft,
  saving,
  currentStage,
  caption,
  allowProgressUpdate = false,
  changed = false,
  onChange,
  onResolveConflict,
  onDiscardConflict,
  onCancel,
  onSave,
  onDelete,
  deleteLabel,
}: {
  draft: HistoryDraft;
  saving: boolean;
  currentStage: ApplicationStage;
  caption?: string;
  allowProgressUpdate?: boolean;
  changed?: boolean;
  onChange: (draft: HistoryDraft) => void;
  onResolveConflict?: () => void;
  onDiscardConflict?: () => void;
  onCancel?: () => void;
  onSave?: () => void;
  onDelete?: () => void;
  deleteLabel?: string;
}) {
  const l = useLocalizer();
  const { locale } = useLocale();
  const columns = localizedColumns(locale);
  const resolvedDeleteLabel = deleteLabel ?? l("删除这条历程", "Delete this history entry");
  const meaningful = historyEntryIsMeaningful({
    ...draft,
    from_stage: currentStage,
    to_stage: draft.update_current_state ? draft.projected_stage : currentStage,
  });
  const projectedStage = draft.update_current_state ? draft.projected_stage : currentStage;
  const canSetNextAction = !TERMINAL_STAGES.includes(projectedStage);
  return (
    <fieldset disabled={saving} className="mb-4 rounded-xl border border-line bg-panel-2/55 p-3 disabled:opacity-70">
      {caption && (
        <div className="mb-2 flex items-center justify-between gap-3 text-xs">
          <span className="font-medium text-ink-2">{caption}</span>
          <span className={changed ? "text-info" : "text-ink-3"}>
            {changed ? l("已修改 · 将随整页保存", "Modified · saved with the page") : l("可直接修改", "Editable")}
          </span>
        </div>
      )}
      {draft.conflicted && onResolveConflict && (
        <div role="alert" className="mb-3 rounded-lg border border-warn/30 bg-warn-soft p-3 text-xs text-ink-2">
          <p className="font-semibold text-warn">
            {draft.entry_conflict ? l("这条历程已在其他位置修改", "This history entry changed elsewhere") : l("岗位状态已在其他位置更新", "The role state changed elsewhere")}
          </p>
          {draft.entry_conflict ? (
            <>
              <p className="mt-1">{l("表单中仍是你的草稿；另一窗口保存的最新值如下：", "The form still contains your draft. The latest values saved in another window are:")}</p>
              <ul className="mt-1 list-disc space-y-0.5 pl-4">
                {draft.entry_conflict.fields.map((field) => {
                  const labels: Record<HistoryEntryEditableField, string> = {
                    step: l("具体环节", "Step"),
                    occurred_date: l("发生日期", "Date"),
                    outcome: l("结果", "Outcome"),
                    summary: l("说明", "Notes"),
                  };
                  const raw = draft.entry_conflict?.latest[field] ?? "";
                  const value = field === "outcome" && raw
                    ? outcomeLabel(raw as Exclude<TimelineOutcome, null>, locale)
                    : raw;
                  return <li key={field}>{labels[field]}{l("：", ": ")}{value || l("（未设置）", "(Not set)")}</li>;
                })}
              </ul>
              <div className="mt-2 flex flex-wrap gap-2">
                <button type="button" onClick={onResolveConflict} className="btn btn-sm">
                  {l("仍用我的草稿覆盖", "Use my draft anyway")}
                </button>
                {onDiscardConflict && (
                  <button type="button" onClick={onDiscardConflict} className="btn btn-sm">
                    {l("放弃这条草稿，使用最新值", "Discard this draft and use the latest values")}
                  </button>
                )}
              </div>
            </>
          ) : (
            <>
              <p className="mt-1">{l("你的历程草稿完整保留，但它会修改当前阶段、环节或下一步。请按上方最新状态核对后明确确认。", "Your draft is intact, but it changes the current stage, step, or next action. Review the latest state above and confirm explicitly.")}</p>
              <button type="button" onClick={onResolveConflict} className="btn btn-sm mt-2">
                {l("基于最新状态继续", "Continue from the latest state")}
              </button>
            </>
          )}
        </div>
      )}
      <div className="grid gap-2 sm:grid-cols-3">
        <label className="text-xs text-ink-3">
          {l("具体环节", "Step")}
          <input
            aria-label={l("历程具体环节", "History step")}
            value={draft.step}
            onChange={(event) => onChange({ ...draft, step: limitCodePoints(event.target.value, 300) })}
            placeholder={l("例如：一面、在线测评", "For example: first interview or assessment")}
            className="input mt-1 w-full"
          />
        </label>
        <label className="text-xs text-ink-3">
          {l("发生日期", "Date")}
          <input
            type="date"
            aria-label={l("历程发生日期", "History date")}
            value={draft.occurred_date}
            onChange={(event) => onChange({ ...draft, occurred_date: event.target.value })}
            className="input mt-1 w-full"
          />
        </label>
        <label className="text-xs text-ink-3">
          {l("结果（可选）", "Outcome (optional)")}
          <select aria-label={l("历程结果", "History outcome")} value={draft.outcome} onChange={(event) => onChange({ ...draft, outcome: event.target.value as HistoryDraft["outcome"] })} className="input mt-1 w-full">
            <option value="">{l("未设置", "Not set")}</option>
            <option value="passed">{l("通过", "Passed")}</option>
            <option value="failed">{l("未通过", "Not passed")}</option>
            <option value="cancelled">{l("取消", "Cancelled")}</option>
          </select>
        </label>
      </div>
      <label className="mt-2 block text-xs text-ink-3">
        {l("说明", "Notes")}
        <textarea
          aria-label={l("历程说明", "History notes")}
          value={draft.summary}
          onChange={(event) => onChange({ ...draft, summary: limitCodePoints(event.target.value, 2000) })}
          placeholder={l("记录发生了什么；不知道的信息可以留空", "Record what happened. Leave unknown details blank.")}
          className="input mt-1 min-h-20 w-full resize-y"
        />
      </label>
      {allowProgressUpdate && (
        <div className="mt-3 rounded-lg border border-line bg-panel px-3 py-2.5">
          <label className="flex cursor-pointer items-start gap-2 text-xs text-ink-2">
            <input type="checkbox" checked={draft.update_current_state} onChange={(event) => onChange({ ...draft, update_current_state: event.target.checked })} className="mt-0.5" />
            <span><strong className="font-medium text-ink">{l("同时更新当前阶段 / 环节", "Also update the current stage and step")}</strong><br />{l("关闭时只补记历史，不会覆盖岗位现在的阶段与环节。", "When off, this only records history and does not overwrite the role's current stage or step.")}</span>
          </label>
          {draft.update_current_state && (
            <label className="mt-2 block text-xs text-ink-3">
              {l("更新后的阶段", "Updated stage")}
              <select aria-label={l("历程更新后的阶段", "Updated stage for history entry")} value={draft.projected_stage} onChange={(event) => {
                const projected_stage = event.target.value as ApplicationStage;
                onChange({
                  ...draft,
                  projected_stage,
                  set_next_action: TERMINAL_STAGES.includes(projected_stage)
                    ? false
                    : draft.set_next_action,
                });
              }} className="input mt-1 w-full">
                {columns.map(([stage, label]) => <option key={stage} value={stage}>{label}</option>)}
              </select>
            </label>
          )}
          {canSetNextAction ? (
            <label className="mt-3 flex cursor-pointer items-center gap-2 text-xs text-ink-2">
              <input type="checkbox" checked={draft.set_next_action} onChange={(event) => onChange({ ...draft, set_next_action: event.target.checked })} />
              {l("同时设置新的下一步", "Also set a new next action")}
            </label>
          ) : (
            <p className="mt-3 text-xs text-ink-3">{l("更新后流程结束，现有下一步会自动清空。", "This update ends the process and automatically clears the existing next action.")}</p>
          )}
          {canSetNextAction && draft.set_next_action && (
            <div className="mt-2 grid gap-2 sm:grid-cols-2">
              <label className="text-xs text-ink-3">{l("完成后阶段（可不变）", "Stage after completion (may stay the same)")}<select aria-label={l("历程下一步完成后阶段", "Stage after the history entry's next action")} value={draft.next_stage} onChange={(event) => onChange({ ...draft, next_stage: event.target.value as ApplicationStage })} className="input mt-1 w-full">{columns.map(([stage, label]) => <option key={stage} value={stage}>{label}</option>)}</select></label>
              <label className="text-xs text-ink-3">{l("下一步", "Next action")} <span className="text-bad">*</span><input aria-label={l("历程下一步", "History next action")} value={draft.next_step} onChange={(event) => onChange({ ...draft, next_step: limitCodePoints(event.target.value, 300) })} className="input mt-1 w-full" /></label>
              <label className="text-xs text-ink-3">{l("日期（可选）", "Date (optional)")}<input type="date" aria-label={l("历程下一步日期", "History next-action date")} value={draft.next_date} onChange={(event) => onChange({ ...draft, next_date: event.target.value, next_time: event.target.value ? draft.next_time : "" })} className="input mt-1 w-full" /></label>
              <label className="text-xs text-ink-3">{l("时间（可选）", "Time (optional)")}<input type="time" aria-label={l("历程下一步时间", "History next-action time")} value={draft.next_time} disabled={!draft.next_date} onChange={(event) => onChange({ ...draft, next_time: event.target.value })} className="input mt-1 w-full" /></label>
              <label className="text-xs text-ink-3 sm:col-span-2">{l("说明（可选）", "Notes (optional)")}<textarea aria-label={l("历程下一步说明", "History next-action notes")} value={draft.next_note} onChange={(event) => onChange({ ...draft, next_note: limitCodePoints(event.target.value, 2000) })} className="input mt-1 min-h-16 w-full resize-y" /></label>
            </div>
          )}
        </div>
      )}
      <div className="mt-2 flex items-center justify-between gap-2">
        <span className={`text-xs ${meaningful ? "text-ink-3" : "text-warn"}`}>
          {meaningful
            ? <span className="tabular-nums">{codePointLength(draft.summary)} / 2000</span>
            : l("至少填写具体环节、结果、说明或阶段/环节变化", "Enter at least a step, outcome, note, or stage/step change")}
        </span>
        <div className="flex gap-2">
          {onDelete && (
            <button type="button" onClick={onDelete} disabled={saving} className="btn btn-sm btn-danger">
              {resolvedDeleteLabel}
            </button>
          )}
          {onCancel && onSave && (
            <>
              <button type="button" onClick={onCancel} disabled={saving} className="btn btn-sm">
                {l("取消", "Cancel")}
              </button>
              <button type="button" onClick={onSave} disabled={saving || !meaningful || (canSetNextAction && draft.set_next_action && !draft.next_step.trim())} className="btn-primary btn-sm">
                {saving ? l("保存中…", "Saving…") : l("保存历程", "Save history entry")}
              </button>
            </>
          )}
        </div>
      </div>
    </fieldset>
  );
}

function TimelineStatisticsDialog({ statistics, loading, error, onRetry, onClose }: {
  statistics: TimelineStatistics | null;
  loading: boolean;
  error: string;
  onRetry: () => void;
  onClose: () => void;
}) {
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const l = useLocalizer();
  const { locale } = useLocale();

  useEffect(() => {
    closeButtonRef.current?.focus();
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  const funnelRows = statistics ? [
    [l("已投递", "Applied"), statistics.funnel.submitted, STAGE_DOT.applied],
    [l("进入笔试", "Reached assessment"), statistics.funnel.written_test, STAGE_DOT.written_test],
    [l("进入面试", "Reached interview"), statistics.funnel.interviewing, STAGE_DOT.interviewing],
    [l("获得 Offer", "Received offer"), statistics.funnel.offer, STAGE_DOT.offer],
    [l("已挂/不再跟进", "Rejected/withdrawn"), statistics.rejected + statistics.withdrawn, STAGE_DOT.rejected],
  ] as const : [];
  const funnelBase = Math.max(statistics?.funnel.submitted ?? 0, 1);
  const formatPercent = (value: number) => formatNumber(
    value / 100,
    locale,
    { maximumFractionDigits: 1, style: "percent" },
  );

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-3 sm:p-6" role="dialog" aria-modal="true" aria-labelledby="timeline-statistics-title">
      <button type="button" tabIndex={-1} aria-label={l("关闭求职数据统计", "Close application statistics")} onClick={onClose} className="absolute inset-0 cursor-default bg-black/45 backdrop-blur-[2px]" />
      <section className="relative flex max-h-[min(90vh,760px)] w-full max-w-3xl flex-col overflow-hidden rounded-[24px] border border-line bg-panel" style={{ boxShadow: "var(--shadow-pop)" }}>
        <header className="flex items-start gap-4 border-b border-line-2 px-5 py-4 sm:px-6 sm:py-5">
          <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-xl border border-info/15 bg-info-soft text-info shadow-[inset_0_1px_0_color-mix(in_srgb,var(--panel)_70%,transparent)]">
            <svg viewBox="0 0 20 20" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round">
              <path d="M3 16.5h14M5 14V9M10 14V4M15 14V7" />
            </svg>
          </div>
          <div className="min-w-0 flex-1">
            <h2 id="timeline-statistics-title" className="text-lg font-semibold tracking-tight">{l("求职数据", "Application statistics")}</h2>
            <p className="mt-0.5 text-sm text-ink-3">{l("基于全部岗位和真实历程实时计算", "Calculated live from every role and its recorded history")}</p>
          </div>
          <button ref={closeButtonRef} type="button" onClick={onClose} aria-label={l("关闭求职数据统计", "Close application statistics")} className="flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-ink-3 transition-colors hover:bg-panel-2 hover:text-ink">
            <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round"><path d="m4 4 8 8M12 4l-8 8" /></svg>
          </button>
        </header>

        <div className="overflow-y-auto px-5 py-5 sm:px-6">
          {loading && (
            <div role="status" className="grid animate-pulse gap-3 sm:grid-cols-3">
              {[0, 1, 2].map((item) => <div key={item} className="h-24 rounded-2xl bg-panel-2" />)}
            </div>
          )}
          {!loading && error && (
            <div role="alert" className="flex flex-col items-center rounded-2xl bg-bad-soft px-5 py-10 text-center">
              <p className="text-sm font-medium text-bad">{error}</p>
              <button type="button" onClick={onRetry} className="btn mt-4">{l("重新加载", "Reload")}</button>
            </div>
          )}
          {!loading && statistics && (
            <>
              <div className="grid gap-3 sm:grid-cols-3">
                {([
                  [l("进行中", "Active"), statistics.active_processes, l("个", ""), l("含泡池中的流程", "Includes pooled processes")],
                  [l("面试", "Interviews"), statistics.funnel.interviewing, l("个", ""), l(`面试转化 ${formatPercent(statistics.interview_conversion_percent)}`, `Interview conversion ${formatPercent(statistics.interview_conversion_percent)}`)],
                  ["Offer", statistics.offers, l("个", ""), l(`Offer 转化 ${formatPercent(statistics.offer_conversion_percent)}`, `Offer conversion ${formatPercent(statistics.offer_conversion_percent)}`)],
                ] as const).map(([label, value, unit, hint]) => (
                  <div key={label} className="rounded-2xl border border-line-2 bg-panel-2/55 p-4">
                    <p className="text-xs font-medium text-ink-3">{label}</p>
                    <p className="mt-2 text-2xl font-semibold tracking-tight tabular-nums">{value}<span className="ml-0.5 text-sm font-normal text-ink-3">{unit}</span></p>
                    <p className="mt-1 text-[11px] leading-4 text-ink-3">{hint}</p>
                  </div>
                ))}
              </div>

              <div className="mt-5 rounded-2xl border border-line p-4 sm:p-5">
                <div className="flex items-baseline justify-between gap-3">
                  <div>
                    <h3 className="text-sm font-semibold">{l("阶段进展", "Stage progression")}</h3>
                    <p className="mt-0.5 text-xs text-ink-3">{l("按历程统计曾经到达的求职阶段", "Stages reached according to recorded history")}</p>
                  </div>
                  <span className="text-xs tabular-nums text-ink-3">{l(`全部岗位 ${statistics.total_positions}`, `${statistics.total_positions} total roles`)}</span>
                </div>
                <div className="mt-4 space-y-3">
                  {funnelRows.map(([label, count, tone]) => {
                    const percent = Math.min(100, count * 100 / funnelBase);
                    const width = count === 0 ? "0%" : `${Math.max(percent, 5)}%`;
                    return (
                      <div key={label} className="grid grid-cols-[7rem_minmax(0,1fr)_2rem] items-center gap-3">
                        <span className="flex items-center gap-2 text-xs text-ink-2">
                          <span aria-hidden="true" className={`h-2 w-2 shrink-0 rounded-full ${tone}`} />
                          {label}
                        </span>
                        <span className="h-2.5 overflow-hidden rounded-full bg-panel-2">
                          <span className={`block h-full rounded-full ${tone}`} style={{ width }} />
                        </span>
                        <span className="text-right text-xs font-semibold tabular-nums">{count}</span>
                      </div>
                    );
                  })}
                </div>
              </div>

              <p className="mt-3 text-[10px] leading-4 text-ink-3">{l("* 转化率以“有效申请”为分母，同一岗位只计算一次。", "* Conversion rates use valid applications as the denominator and count each role once.")}</p>
            </>
          )}
        </div>
      </section>
    </div>
  );
}

function TimelineBoard({ board, search, selectedId, pendingIds, notices, onSelect, onMove }: {
  board: Board;
  search: string;
  selectedId: number | null;
  pendingIds: ReadonlySet<number>;
  notices: Readonly<Record<number, StageMoveNotice>>;
  onSelect: (id: number) => void;
  onMove: (item: BoardItem, targetStage: ApplicationStage) => void;
}) {
  const l = useLocalizer();
  const { locale } = useLocale();
  const columns = localizedColumns(locale);
  const [draggingId, setDraggingId] = useState<number | null>(null);
  const [dropTarget, setDropTarget] = useState<ApplicationStage | null>(null);
  const [collapsedStages, setCollapsedStages] = useState<Set<ApplicationStage>>(
    () => new Set(RAIL_STAGES),
  );
  const suppressClickRef = useRef(false);
  const filterItems = (key: ApplicationStage) => sortByPriorityAndCreatedTime(
    (board.columns[key] ?? []).filter((item) => matchesTimelineQuery(item, search)),
  );

  if (search.trim() && Object.values(board.columns).every(
    (items) => items.every((item) => !matchesTimelineQuery(item, search)),
  )) {
    return <p className="card px-4 py-8 text-center text-sm text-ink-3">{l("没有匹配的岗位", "No matching roles")}</p>;
  }

  function startDrag(event: DragEvent<HTMLButtonElement>, item: BoardItem) {
    if (pendingIds.has(item.id)) {
      event.preventDefault();
      return;
    }
    suppressClickRef.current = true;
    setDraggingId(item.id);
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("application/x-careerdesk-id", String(item.id));
    event.dataTransfer.setData("text/plain", String(item.id));
  }

  function finishDrag() {
    setDraggingId(null);
    setDropTarget(null);
    window.setTimeout(() => { suppressClickRef.current = false; }, 0);
  }

  function dropCard(event: DragEvent<HTMLElement>, targetStage: ApplicationStage) {
    event.preventDefault();
    const rawId = event.dataTransfer.getData("application/x-careerdesk-id")
      || event.dataTransfer.getData("text/plain");
    const applicationId = Number(rawId);
    const item = Object.values(board.columns).flat().find(
      (candidate) => candidate.id === applicationId,
    );
    // An optimistic update immediately unmounts the drag source, so onDragEnd will not fire.
    // Clear drag state during drop to avoid leaving the placeholder or translucent card behind.
    setDraggingId(null);
    setDropTarget(null);
    window.setTimeout(() => { suppressClickRef.current = false; }, 0);
    if (!item || item.stage === targetStage || pendingIds.has(item.id)) return;
    onMove(item, targetStage);
  }

  const dragProps = (key: ApplicationStage) => ({
    onDragEnter: () => { if (draggingId !== null) setDropTarget(key); },
    onDragOver: (event: DragEvent<HTMLElement>) => {
      if (draggingId === null) return;
      event.preventDefault();
      event.dataTransfer.dropEffect = "move";
      setDropTarget(key);
    },
    onDrop: (event: DragEvent<HTMLElement>) => dropCard(event, key),
  });

  function renderCard(item: BoardItem) {
    const notice = notices[item.id];
    return (
      <div
        key={item.id}
        aria-busy={pendingIds.has(item.id)}
        className={`relative shrink-0 overflow-hidden rounded-xl border bg-panel transition-colors hover:border-line-strong ${
          selectedId === item.id ? "border-accent" : "border-line"
        } ${draggingId === item.id ? "opacity-45" : ""} ${
          pendingIds.has(item.id) ? "cursor-wait" : ""
        }`}
      >
        <button
          type="button"
          draggable={!pendingIds.has(item.id)}
          onDragStart={(event) => startDrag(event, item)}
          onDragEnd={finishDrag}
          onClick={() => { if (!suppressClickRef.current) onSelect(item.id); }}
          title={`${item.company}·${item.position}`}
          className="flex w-full cursor-grab items-center gap-1 px-2 py-2 text-left active:cursor-grabbing"
        >
          {item.priority && (
            <span className={`tag shrink-0 px-1 py-0 text-[10px] ${PRIORITY_META[item.priority].badgeClass}`}>
              {l(PRIORITY_META[item.priority].label, item.priority === "high" ? "High" : item.priority === "medium" ? "Medium" : "Low")}
            </span>
          )}
          <span className="min-w-0 flex-1 truncate text-[13px]">
            <span className="font-semibold">{item.company}</span>
            <span aria-hidden="true" className="text-ink-3"> · </span>
            <span className="text-ink-2">{item.position}</span>
          </span>
          {(pendingIds.has(item.id) || notice) && (
          <span
            role={notice?.kind === "error" ? "alert" : "status"}
            aria-live="polite"
            className={`ml-1 inline-flex shrink-0 items-center gap-1 rounded-md border px-1.5 py-0.5 text-[11px] font-semibold ${
              notice?.kind === "error"
                ? "border-bad/20 bg-bad/10 text-bad"
                : notice?.kind === "saved"
                  ? "border-ok/20 bg-ok/10 text-ok"
                  : "border-info/20 bg-info/10 text-info"
            }`}
          >
            <span aria-hidden="true" className={notice ? "text-base" : "h-2 w-2 animate-pulse rounded-full bg-info"}>
              {notice?.kind === "error" ? "×" : notice?.kind === "saved" ? "✓" : ""}
            </span>
            <span>{notice?.message ?? l("保存中…", "Saving…")}</span>
          </span>
          )}
        </button>
      </div>
    );
  }

  const columnDot = (key: ApplicationStage, dim: boolean) =>
    <span className={`h-2 w-2 shrink-0 rounded-full ${STAGE_DOT[key] ?? "bg-ink-3"} ${dim ? "opacity-40" : ""}`} />;

  return (
    <div className="flex items-stretch gap-2 overflow-x-auto pb-2 md:h-full md:min-h-0">
      {columns.map(([key]) => {
        const items = filterItems(key);
        const empty = items.length === 0;
        const temporarilyExpanded = draggingId !== null && dropTarget === key;
        const collapsed = collapsedStages.has(key) && !temporarilyExpanded;
        if (!collapsed) {
          return (
            <div
              key={key}
              {...dragProps(key)}
              className={`flex min-w-[190px] flex-1 basis-0 flex-col rounded-xl border p-1.5 transition-colors md:h-full md:min-h-0 ${
                dropTarget === key ? "border-info bg-info-soft/50" : "border-transparent bg-panel-2/30"
              }`}
            >
              <div className="mb-2 flex items-center gap-1.5 px-1">
                {columnDot(key, empty)}
                <span className={`truncate text-sm font-medium ${empty ? "text-ink-3" : ""}`}>{stageLabel(key, locale)}</span>
                <span className="text-xs tabular-nums text-ink-3">{items.length}</span>
                <button
                  type="button"
                  onClick={() => setCollapsedStages((current) => new Set([...current, key]))}
                  aria-label={l(`收起${stageLabel(key)}阶段`, `Collapse ${stageLabel(key, locale)} stage`)}
                  className="ml-auto inline-flex items-center gap-0.5 rounded-md px-1.5 py-0.5 text-xs text-ink-3 transition-colors hover:bg-panel-2 hover:text-ink"
                >
                  {l("收起", "Collapse")}
                  <svg viewBox="0 0 16 16" className="h-3 w-3" fill="none" stroke="currentColor" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round"><path d="m6 4 4 4-4 4" /></svg>
                </button>
              </div>
              {empty ? (
                draggingId !== null ? (
                  <div className="flex min-h-20 flex-1 items-center justify-center rounded-lg border border-dashed border-line bg-panel-2/60 text-center text-xs text-ink-3">
                    {l("拖到这里", "Drop here")}
                  </div>
                ) : <p className="px-1 py-2 text-xs text-ink-3">{l("暂无", "None")}</p>
              ) : (
                <div className="flex max-h-[64vh] flex-col gap-2 overflow-y-auto pr-1 md:max-h-none md:min-h-0 md:flex-1">
                  {items.map(renderCard)}
                </div>
              )}
            </div>
          );
        }
        return (
          <button
            key={key}
            type="button"
            onClick={() => setCollapsedStages((current) => {
              const next = new Set(current);
              next.delete(key);
              return next;
            })}
            {...dragProps(key)}
            aria-label={l(`展开${stageLabel(key)}阶段，共 ${items.length} 个岗位`, `Expand ${stageLabel(key, locale)} stage, ${items.length} roles`)}
            title={l(`${stageLabel(key)} · ${items.length}（点击展开）`, `${stageLabel(key, locale)} · ${items.length} (click to expand)`)}
            className={`flex w-10 shrink-0 flex-col items-center gap-2 rounded-xl border border-dashed py-3 text-ink-3 transition-colors hover:border-line-strong hover:text-ink md:h-full ${
              dropTarget === key ? "border-info text-info" : "border-line-strong"
            }`}
          >
            <span style={{ writingMode: "vertical-rl" }} className="text-xs tracking-widest">{stageLabel(key, locale)}</span>
            <span className="text-xs tabular-nums">{items.length}</span>
          </button>
        );
      })}
    </div>
  );
}
