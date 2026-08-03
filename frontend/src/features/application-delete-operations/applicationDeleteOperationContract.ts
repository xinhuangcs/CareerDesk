import type {
  ApplicationNextAction,
  ApplicationPriority,
  ApplicationStage,
} from "../applications/applicationContract";

export type ApplicationDeleteOperationState = "pending" | "completed" | "rejected" | "stale";

export type ApplicationDeleteTimelineEntry = {
  id: number;
  step: string | null;
  occurred_date: string | null;
  outcome: "passed" | "failed" | "cancelled" | null;
  summary: string | null;
  from_stage: ApplicationStage | null;
  from_step: string | null;
  to_stage: ApplicationStage | null;
  to_step: string | null;
};

type ApplicationDeleteOperationBase = {
  operation_id: string;
  operation_type: "application_delete";
  created_time: string;
  target: {
    application_id: number;
    company: string;
    position: string;
    department: string | null;
    channel: string | null;
    stage: ApplicationStage;
    current_step: string | null;
    priority: ApplicationPriority;
    selected_resume: { id: number; name: string; archived: boolean } | null;
    applied_date: string | null;
    next_action: ApplicationNextAction | null;
    paused_from_stage: ApplicationStage | null;
    pause_reason: string | null;
    application_note: string | null;
    jd_preview: string;
    jd_truncated: boolean;
    skills: string[];
    highlights: string[];
    prep_status: "none" | "pending" | "running" | "ready" | "failed";
    prep_artifact_present: boolean;
    application_created_time: string;
    application_updated_time: string;
  };
  effect: {
    timeline_entries: ApplicationDeleteTimelineEntry[];
    questions_detached: {
      id: number;
      text_preview: string;
      text_truncated: boolean;
      source: "real" | "generated" | "imported";
    }[];
    question_occurrences_detached: number;
    resumes_detached: {
      id: number;
      name: string;
      binding: "family" | "application";
      archived: boolean;
    }[];
    selected_resume_retained: true;
    company_records_untouched: true;
    journal_records_untouched: true;
    external_logs_untouched: true;
  };
};

export type ApplicationDeleteResult = {
  status: "ok";
  application_id: number;
  timeline_entries_removed: number;
  questions_detached: number;
  question_occurrences_detached: number;
  resumes_detached: number;
};

export type ApplicationDeleteOperation = ApplicationDeleteOperationBase & (
  | { state: "completed"; result: ApplicationDeleteResult }
  | { state: Exclude<ApplicationDeleteOperationState, "completed">; result: null }
);
