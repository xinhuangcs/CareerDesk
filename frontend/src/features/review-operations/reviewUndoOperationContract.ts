import type {
  ApplicationNextAction,
  ApplicationStage,
} from "../applications/applicationContract";

export type ReviewUndoOperationState = "pending" | "completed" | "rejected" | "stale";

export type ReviewUndoTimelineEntry = {
  id: number;
  step: string | null;
  occurred_date: string | null;
  outcome: "passed" | "failed" | "cancelled" | null;
  summary: string | null;
  from_stage: ApplicationStage;
  from_step: string | null;
  to_stage: ApplicationStage;
  to_step: string | null;
};

export type ReviewUndoApplicationProjection = {
  stage: ApplicationStage;
  current_step: string | null;
  current_state_entry_id: number | null;
  next_action: ApplicationNextAction | null;
  paused_from_stage: ApplicationStage | null;
  pause_reason: string | null;
  channel: string | null;
  applied_date: string | null;
  revision: number;
};

// Whole-review undo is high risk: the agent freezes target/effect and the trusted page
// submits only the server operation ID. The server closes target/dependency drift as stale.
export type ReviewUndoOperation = {
  operation_id: string;
  state: ReviewUndoOperationState;
  created_time: string;
  target: {
    journal_id: number;
    expected_revision: number;
    company: string;
    position: string;
    content_preview: string;
    content_truncated: boolean;
    review_created_time: string;
  };
  effect: {
    timeline_entries: ReviewUndoTimelineEntry[];
    status_logs_removed: number;
    questions_archived: { id: number; text: string }[];
    application: {
      id: number | null;
      company: string;
      position: string;
      record_exists: boolean;
      record_retained: boolean;
      expected: ReviewUndoApplicationProjection | null;
      replacement: ReviewUndoApplicationProjection | null;
    };
  };
  result: {
    status: "ok";
    target_revision: number;
    application_id: number | null;
    application_stage: ApplicationStage | null;
    removed: {
      timeline_entries: number;
      status_logs: number;
      questions_archived: number;
    };
  } | null;
};
