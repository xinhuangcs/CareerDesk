import type {
  ApplicationNextAction,
  ApplicationPriority,
  ApplicationStage,
} from "../applications/applicationContract";

export type ApplicationMergeOperationState = "pending" | "completed" | "rejected" | "stale";

export type ApplicationMergeResumeRef = {
  id: number;
  name: string;
  binding: "family" | "application";
  application_id: number | null;
  archived: boolean;
};

export type ApplicationMergeApplication = {
  application_id: number;
  revision: number;
  company: string;
  position: string;
  department: string | null;
  channel: string | null;
  stage: ApplicationStage;
  current_step: string | null;
  priority: ApplicationPriority;
  selected_resume: ApplicationMergeResumeRef | null;
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

export type ApplicationMergeFinalDestination = {
  application_id: number;
  company: string;
  position: string;
  department: string | null;
  channel: string | null;
  stage: ApplicationStage;
  current_step: string | null;
  priority: ApplicationPriority;
  selected_resume: ApplicationMergeResumeRef | null;
  applied_date: string | null;
  next_action: ApplicationNextAction | null;
  paused_from_stage: ApplicationStage | null;
  pause_reason: string | null;
  application_note: string | null;
  jd_source: "source" | "destination" | "none";
  jd_preview: string;
  jd_truncated: boolean;
  skills: string[];
  highlights: string[];
  prep_status: "none";
  prep_artifact_present: false;
};

export type ApplicationMergeFieldResolution = {
  field:
    | "company"
    | "position"
    | "department"
    | "channel"
    | "stage"
    | "current_step"
    | "priority"
    | "selected_resume"
    | "applied_date"
    | "next_action"
    | "pause"
    | "application_note"
    | "jd"
    | "prep";
  strategy:
    | "destination_identity"
    | "destination_preferred"
    | "source_fallback"
    | "highest_priority"
    | "cleared_for_safety";
  source_value: string | null;
  destination_value: string | null;
  final_value: string | null;
  source_value_carried_forward: boolean;
};

export type ApplicationMergeCounts = {
  timeline_entries: number;
  questions: number;
  question_occurrences: number;
  resumes: number;
};

type ApplicationMergeOperationBase = {
  operation_id: string;
  operation_type: "application_merge";
  created_time: string;
  source: ApplicationMergeApplication;
  destination: ApplicationMergeApplication;
  effect: {
    final_destination: ApplicationMergeFinalDestination;
    field_resolutions: ApplicationMergeFieldResolution[];
    timeline_entries_rebound: {
      id: number;
      step: string | null;
      occurred_date: string | null;
      outcome: "passed" | "failed" | "cancelled" | null;
      summary: string | null;
      from_stage: ApplicationStage;
      from_step: string | null;
      to_stage: ApplicationStage;
      to_step: string | null;
      source: "manual" | "agent" | "review" | "drag" | "system";
      journal_id: number | null;
      created_time: string;
    }[];
    questions_rebound: {
      id: number;
      text_preview: string;
      text_truncated: boolean;
      source: "real" | "generated" | "imported";
      company_before: string | null;
      company_after: string;
      journal_id: number | null;
    }[];
    question_occurrences_rebound: {
      journal_id: number;
      question_id: number;
      company_before: string;
      company_after: string;
    }[];
    resumes_rebound: {
      id: number;
      name: string;
      binding: "application";
      archived: boolean;
    }[];
    destination_existing: ApplicationMergeCounts;
    source_application_removed: true;
    destination_prep_reset: true;
    source_prep_removed_with_application: true;
    company_records_untouched: true;
    journal_records_untouched: true;
    external_logs_untouched: true;
  };
};

export type ApplicationMergeResult = {
  status: "ok";
  source_application_id: number;
  destination_application_id: number;
  source_deleted: true;
  moved: ApplicationMergeCounts;
  destination_totals: ApplicationMergeCounts;
  destination_prep_reset: true;
  final_destination: ApplicationMergeFinalDestination;
};

export type ApplicationMergeOperation = ApplicationMergeOperationBase & (
  | { state: "completed"; result: ApplicationMergeResult }
  | { state: Exclude<ApplicationMergeOperationState, "completed">; result: null }
);
