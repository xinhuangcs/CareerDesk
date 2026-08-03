export type ResumeItem = {
  id: number;
  name: string;
  binding: string;
  application_id: number | null;
  application_company: string | null;
  application_position: string | null;
  archived: boolean;
  content_hash: string;
  character_count: number;
  annotation_status: "pending" | "ready" | "failed";
  updated_time: string;
};

export type ResumeText = {
  id: number;
  name: string;
  content_text: string;
  content_hash: string;
  character_count: number;
  updated_time: string;
};

export type ResumeJob = {
  job_id: string;
  operation: "create" | "update";
  target_resume_id: number | null;
  name: string;
  state: "processing" | "completed" | "failed";
  stage: "queued" | "extracting" | "parsing" | "saving" | "completed" | "failed";
  message: string | null;
  resume_id: number | null;
  created_time: string;
  updated_time: string;
};

export type ResumeJobDismissResponse = {
  status: "ok";
  dismissed: boolean;
};
