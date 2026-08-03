export type ApplicationStage =
  | "backlog"
  | "applied"
  | "written_test"
  | "interviewing"
  | "offer"
  | "withdrawn"
  | "rejected"
  | "pooled";

export type ApplicationPriority = "high" | "medium" | "low" | null;

export type ApplicationNextAction = {
  stage: ApplicationStage;
  step: string;
  date: string | null;
  time: string | null;
  note: string | null;
};

export type ApplicationTimelineEntry = {
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
  created_time: string;
  display_time: string;
  snapshot_fingerprint: string;
};
