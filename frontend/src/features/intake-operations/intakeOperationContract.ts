import type {
  ApplicationNextAction,
  ApplicationPriority,
  ApplicationStage,
} from "../applications/applicationContract";

export type IntakeOperationState = "pending" | "completed" | "rejected" | "stale";

export type IntakePosition = {
  mode: "create" | "update";
  company: string;
  position: string;
  department: string | null;
  channel: string | null;
  stage: ApplicationStage;
  current_step: string | null;
  applied_date: string | null;
  pause_reason: string | null;
  next_action: ApplicationNextAction | null;
  application_note: string | null;
  priority: ApplicationPriority;
  jd_text: string | null;
  skills: string[];
  highlights: string[];
  flags: {
    invalidate_prep: boolean;
    add_applied_entry: boolean;
    clear_next_action: boolean;
  };
  already_exists: boolean;
};

export type IntakeOperation = {
  operation_id: string;
  state: IntakeOperationState;
  positions: IntakePosition[];
  source_rows: number;
  skipped_rows: number;
  created_time: string;
  exclude_indexes?: number[] | null;
  result?: Record<string, unknown> | null;
};
