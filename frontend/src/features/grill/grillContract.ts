export type GrillProgress = { answered: number; total: number };

export type GrillQuestion = {
  id: number;
  text: string;
  category: string;
  channel: "interview" | "written";
  response_format: string;
  difficulty: string;
  primary_competency: string;
  secondary_tags: string[];
};

export type GrillFlowResponse = {
  status: "ok" | "error" | "processing" | "finished" | "suspended";
  session_id?: number;
  question?: GrillQuestion;
  follow_up?: string;
  ack?: string;
  progress?: GrillProgress;
  summary?: Record<string, unknown>;
  code?: string;
  message?: string;
};

export type SessionListItem = {
  id: number;
  question_set_id: number;
  kind: "generated" | "library_snapshot";
  edition: "basic" | "custom" | null;
  context_label: string;
  state: "active" | "suspended" | "finished";
  answered: number;
  total: number;
  started_time: string;
  ended_time: string | null;
};

export type ReplayAnswer = {
  session_item_id: number;
  text: string;
  category: string;
  verdict: "meets" | "partially_meets" | "needs_work" | "ungradable" | "skipped";
  stuck: boolean;
  feedback: Record<string, unknown>;
  answer_guide: Record<string, unknown>;
  primary_competency: string;
  transcript: Record<string, unknown>[];
};

export type SessionReplay = {
  status: "ok" | "error" | "processing";
  session_id?: number;
  context_label?: string;
  kind?: string;
  edition?: string;
  answers?: ReplayAnswer[];
  summary?: Record<string, unknown>;
  code?: string;
  message?: string;
};

export type QuestionSetItem = {
  id: number;
  kind: "generated" | "library_snapshot";
  edition: "basic" | "custom" | null;
  resume_id: number | null;
  application_id: number | null;
  state: "pending" | "running" | "ready" | "failed";
  stage: string;
  safe_error_code: string | null;
  context_label: string;
  archived_at: string | null;
  question_count: number;
  unpracticed_count: number;
  created_time: string;
  updated_time: string;
  content_locale: "zh-CN" | "en";
  currentness: "current" | "stale" | "legacy" | "fixed" | "not_ready";
};

export type ReadinessResume = {
  id: number;
  name: string;
  family: string | null;
  binding: string;
  application_id: number | null;
  archived: boolean;
  annotation_status: string;
  character_count: number | null;
};

function object(value: unknown, label: string): Record<string, unknown> {
  if (!value || typeof value !== "object" || Array.isArray(value)) throw new Error(`${label} has an invalid response shape`);
  return value as Record<string, unknown>;
}

function exact(value: Record<string, unknown>, allowed: string[], label: string) {
  const extras = Object.keys(value).filter((key) => !allowed.includes(key));
  if (extras.length) throw new Error(`${label} contains unknown fields: ${extras.join(", ")}`);
}

function number(value: unknown, label: string): number {
  if (typeof value !== "number" || !Number.isFinite(value)) throw new Error(`${label} must be a number`);
  return value;
}

function integer(value: unknown, label: string, minimum = 0): number {
  const parsed = number(value, label);
  if (!Number.isInteger(parsed) || parsed < minimum) throw new Error(`${label} must be a valid integer`);
  return parsed;
}

function boolean(value: unknown, label: string): boolean {
  if (typeof value !== "boolean") throw new Error(`${label} must be a boolean`);
  return value;
}

function string(value: unknown, label: string): string {
  if (typeof value !== "string") throw new Error(`${label} must be text`);
  return value;
}

function nullableString(value: unknown, label: string): string | null {
  return value === null ? null : string(value, label);
}

function parseQuestion(value: unknown): GrillQuestion {
  const item = object(value, "question");
  exact(item, ["id", "text", "category", "channel", "response_format", "difficulty", "primary_competency", "secondary_tags"], "question");
  if (item.channel !== "interview" && item.channel !== "written") throw new Error("question channel is invalid");
  if (!Array.isArray(item.secondary_tags) || item.secondary_tags.some((tag) => typeof tag !== "string")) throw new Error("question tags are invalid");
  return { id: number(item.id, "question id"), text: string(item.text, "question text"), category: string(item.category, "question category"), channel: item.channel, response_format: string(item.response_format, "response format"), difficulty: string(item.difficulty, "difficulty"), primary_competency: string(item.primary_competency, "primary competency"), secondary_tags: item.secondary_tags as string[] };
}

export function parseGrillFlowResponse(value: unknown): GrillFlowResponse {
  const item = object(value, "practice");
  exact(item, ["status", "session_id", "question", "follow_up", "ack", "progress", "summary", "code", "message"], "practice");
  if (!["ok", "error", "processing", "finished", "suspended"].includes(String(item.status))) throw new Error("practice status is invalid");
  const progress = item.progress === undefined ? undefined : object(item.progress, "practice progress");
  if (progress) exact(progress, ["answered", "total"], "practice progress");
  return {
    status: item.status as GrillFlowResponse["status"],
    ...(item.session_id === undefined ? {} : { session_id: number(item.session_id, "session id") }),
    ...(item.question === undefined ? {} : { question: parseQuestion(item.question) }),
    ...(item.follow_up === undefined ? {} : { follow_up: string(item.follow_up, "follow-up") }),
    ...(item.ack === undefined ? {} : { ack: string(item.ack, "acknowledgement") }),
    ...(progress ? { progress: { answered: number(progress.answered, "answered count"), total: number(progress.total, "total count") } } : {}),
    ...(item.summary === undefined ? {} : { summary: object(item.summary, "session summary") }),
    ...(item.code === undefined ? {} : { code: string(item.code, "error code") }),
    ...(item.message === undefined ? {} : { message: string(item.message, "error message") }),
  };
}

function parseQuestionSet(value: unknown): QuestionSetItem {
  const item = object(value, "question set");
  exact(item, ["id", "kind", "edition", "resume_id", "application_id", "state", "stage", "safe_error_code", "context_label", "archived_at", "question_count", "unpracticed_count", "created_time", "updated_time", "currentness", "content_locale"], "question set");
  if (item.kind !== "generated" && item.kind !== "library_snapshot") throw new Error("question-set type is invalid");
  if (!["pending", "running", "ready", "failed"].includes(String(item.state))) throw new Error("question-set state is invalid");
  if (!["current", "stale", "legacy", "fixed", "not_ready"].includes(String(item.currentness))) throw new Error("question-set currentness is invalid");
  if (item.edition !== null && item.edition !== "basic" && item.edition !== "custom") throw new Error("question-set edition is invalid");
  const contentLocale = item.content_locale ?? "zh-CN";
  if (contentLocale !== "zh-CN" && contentLocale !== "en") throw new Error("question-set locale is invalid");
  return { id: integer(item.id, "question-set id", 1), kind: item.kind, edition: item.edition as "basic" | "custom" | null, resume_id: item.resume_id === null ? null : integer(item.resume_id, "résumé id", 1), application_id: item.application_id === null ? null : integer(item.application_id, "application id", 1), state: item.state as QuestionSetItem["state"], stage: string(item.stage, "question-set stage"), safe_error_code: nullableString(item.safe_error_code, "safe error code"), context_label: string(item.context_label, "question-set label"), archived_at: nullableString(item.archived_at, "archive time"), question_count: integer(item.question_count, "question count"), unpracticed_count: integer(item.unpracticed_count, "unpracticed count"), created_time: string(item.created_time, "created time"), updated_time: string(item.updated_time, "updated time"), currentness: item.currentness as QuestionSetItem["currentness"], content_locale: contentLocale };
}

export function parseReadinessResponse(value: unknown): ReadinessResponse {
  const root = object(value, "interview readiness");
  exact(root, ["resumes", "applications", "question_sets", "model_configured", "selection"], "interview readiness");
  if (!Array.isArray(root.resumes) || !Array.isArray(root.question_sets) || typeof root.model_configured !== "boolean") throw new Error("interview-readiness lists are invalid");
  const resumes = root.resumes.map((raw) => {
    const item = object(raw, "résumé summary");
    exact(item, ["id", "name", "family", "binding", "application_id", "archived", "annotation_status", "character_count"], "résumé summary");
    return { id: integer(item.id, "résumé id", 1), name: string(item.name, "résumé name"), family: nullableString(item.family, "résumé family"), binding: string(item.binding, "résumé binding"), application_id: item.application_id === null ? null : integer(item.application_id, "application id", 1), archived: boolean(item.archived, "résumé archive flag"), annotation_status: string(item.annotation_status, "annotation status"), character_count: item.character_count === null ? null : integer(item.character_count, "résumé character count") };
  });
  const applications = object(root.applications, "application summary");
  exact(applications, ["columns", "total"], "application summary");
  const columns = object(applications.columns, "application groups");
  const parsedColumns: Record<string, ReadinessApplication[]> = {};
  for (const [stage, values] of Object.entries(columns)) {
    if (!Array.isArray(values)) throw new Error("application group is invalid");
    parsedColumns[stage] = values.map((raw) => {
      const item = object(raw, "application"); exact(item, ["id", "company", "position"], "application");
      return { id: integer(item.id, "application id", 1), company: string(item.company, "company"), position: string(item.position, "role") };
    });
  }
  let selection = root.selection as ReadinessResponse["selection"];
  if (selection !== undefined) {
    const selected = object(selection, "current selection");
    exact(selected, ["ready", "code", "message", "capacity", "context_label", "requirements"], "current selection");
    const capacity = selected.capacity === null || selected.capacity === undefined ? undefined : object(selected.capacity, "capacity");
    if (capacity) {
      exact(capacity, ["state", "code", "effective_question_limit", "compressed_materials", "extra_calls"], "capacity");
      if (!["direct", "compressed", "blocked"].includes(String(capacity.state))) throw new Error("capacity state is invalid");
      integer(capacity.effective_question_limit, "question limit");
      integer(capacity.extra_calls, "extra call count");
      if (!Array.isArray(capacity.compressed_materials) || capacity.compressed_materials.some((kind) => typeof kind !== "string")) throw new Error("compressed-material list is invalid");
    }
    const requirements = selected.requirements === undefined ? undefined : object(selected.requirements, "readiness requirements");
    if (requirements) {
      exact(requirements, ["resume", "jd"], "readiness requirements");
      for (const [kind, raw] of Object.entries(requirements)) {
        const requirement = object(raw, `${kind} requirement`);
        exact(requirement, ["ready", "label", "character_count", "present"], `${kind} requirement`);
        for (const key of ["ready", "present"]) if (requirement[key] !== undefined) boolean(requirement[key], `${kind}.${key}`);
        if (requirement.label !== undefined && requirement.label !== null) string(requirement.label, `${kind}.label`);
        if (requirement.character_count !== undefined && requirement.character_count !== null) integer(requirement.character_count, `${kind}.character_count`);
      }
    }
    selection = {
      ready: boolean(selected.ready, "current selection ready"),
      ...(selected.code === undefined || selected.code === null ? {} : { code: string(selected.code, "current-selection error code") }),
      ...(selected.message === undefined || selected.message === null ? {} : { message: string(selected.message, "current-selection message") }),
      ...(capacity ? { capacity: capacity as ReadinessResponse["selection"] extends { capacity?: infer C } ? C : never } : {}),
      ...(requirements ? { requirements: requirements as ReadinessResponse["selection"] extends { requirements?: infer R } ? R : never } : {}),
    };
  }
  return { resumes, applications: { columns: parsedColumns, total: number(applications.total, "application count") }, question_sets: root.question_sets.map(parseQuestionSet), model_configured: root.model_configured, ...(selection === undefined ? {} : { selection }) };
}

export function parseSessionsResponse(value: unknown): SessionListItem[] {
  const root = object(value, "session list"); exact(root, ["items"], "session list");
  if (!Array.isArray(root.items)) throw new Error("session list is invalid");
  return root.items.map((raw) => {
    const item = object(raw, "session"); exact(item, ["id", "question_set_id", "kind", "edition", "context_label", "state", "answered", "total", "started_time", "ended_time"], "session");
    if (item.kind !== "generated" && item.kind !== "library_snapshot") throw new Error("session type is invalid");
    if (item.edition !== null && item.edition !== "basic" && item.edition !== "custom") throw new Error("session edition is invalid");
    if (!["active", "suspended", "finished"].includes(String(item.state))) throw new Error("session state is invalid");
    return { id: integer(item.id, "session id", 1), question_set_id: integer(item.question_set_id, "question-set id", 1), kind: item.kind, edition: item.edition as SessionListItem["edition"], context_label: string(item.context_label, "session label"), state: item.state as SessionListItem["state"], answered: integer(item.answered, "answered count"), total: integer(item.total, "total count"), started_time: string(item.started_time, "start time"), ended_time: nullableString(item.ended_time, "end time") };
  });
}

export function parseReplayResponse(value: unknown): SessionReplay {
  const root = object(value, "session replay");
  exact(root, ["status", "session_id", "context_label", "kind", "edition", "answers", "summary", "code", "message"], "session replay");
  if (!["ok", "error", "processing"].includes(String(root.status))) throw new Error("session-replay status is invalid");
  const answers = root.answers === undefined ? undefined : root.answers;
  if (answers !== undefined && !Array.isArray(answers)) throw new Error("session-replay answers are invalid");
  const parsedAnswers = answers?.map((raw) => {
    const item = object(raw, "session-replay answer");
    exact(item, ["session_item_id", "text", "category", "verdict", "stuck", "feedback", "answer_guide", "primary_competency", "transcript"], "session-replay answer");
    if (!["meets", "partially_meets", "needs_work", "ungradable", "skipped"].includes(String(item.verdict))) throw new Error("session-replay verdict is invalid");
    if (!Array.isArray(item.transcript) || item.transcript.some((part) => !part || typeof part !== "object" || Array.isArray(part))) throw new Error("session-replay transcript is invalid");
    return { session_item_id: integer(item.session_item_id, "session-item id", 1), text: string(item.text, "replay question text"), category: string(item.category, "replay category"), verdict: item.verdict as ReplayAnswer["verdict"], stuck: boolean(item.stuck, "stuck flag"), feedback: object(item.feedback, "feedback"), answer_guide: object(item.answer_guide, "answer guide"), primary_competency: string(item.primary_competency, "primary competency"), transcript: item.transcript as Record<string, unknown>[] };
  });
  return { status: root.status as SessionReplay["status"], ...(root.session_id === undefined || root.session_id === null ? {} : { session_id: integer(root.session_id, "session id", 1) }), ...(root.context_label === undefined || root.context_label === null ? {} : { context_label: string(root.context_label, "session label") }), ...(root.kind === undefined || root.kind === null ? {} : { kind: string(root.kind, "session type") }), ...(root.edition === undefined || root.edition === null ? {} : { edition: string(root.edition, "session edition") }), ...(parsedAnswers === undefined ? {} : { answers: parsedAnswers }), ...(root.summary === undefined || root.summary === null ? {} : { summary: object(root.summary, "session summary") }), ...(root.code === undefined || root.code === null ? {} : { code: string(root.code, "error code") }), ...(root.message === undefined || root.message === null ? {} : { message: string(root.message, "error message") }) };
}

export function parseStatusResponse(value: unknown): { status: string; question_set_id?: number; code?: string; message?: string } {
  const root = object(value, "command");
  exact(root, ["status", "question_set_id", "code", "message"], "command");
  if (!["processing", "ready", "error"].includes(String(root.status))) throw new Error("command status is invalid");
  return {
    status: string(root.status, "command status"),
    ...(root.question_set_id === undefined ? {} : { question_set_id: number(root.question_set_id, "question-set id") }),
    ...(root.code === undefined ? {} : { code: string(root.code, "error code") }),
    ...(root.message === undefined ? {} : { message: string(root.message, "error message") }),
  };
}

export function parseDeleteSetResponse(value: unknown): { status: "deleted" | "archived" | "not_found" } {
  const root = object(value, "question-set deletion"); exact(root, ["status"], "question-set deletion");
  if (!["deleted", "archived", "not_found"].includes(String(root.status))) throw new Error("question-set deletion status is invalid");
  return { status: root.status as "deleted" | "archived" | "not_found" };
}

export function parseMutationResponse(value: unknown): { status: "ok" | "error"; content_hash?: string; message?: string } {
  const root = object(value, "operation"); exact(root, ["status", "content_hash", "message"], "operation");
  if (root.status !== "ok" && root.status !== "error") throw new Error("operation status is invalid");
  return { status: root.status, ...(root.content_hash === undefined || root.content_hash === null ? {} : { content_hash: string(root.content_hash, "content hash") }), ...(root.message === undefined || root.message === null ? {} : { message: string(root.message, "operation message") }) };
}

export type ReadinessApplication = {
  id: number;
  company: string;
  position: string;
  resume_id?: number | null;
};

export type ReadinessRequirement = {
  ready?: boolean;
  label?: string | null;
  character_count?: number | null;
  present?: boolean;
};

export type ReadinessResponse = {
  resumes: ReadinessResume[];
  applications: { columns: Record<string, ReadinessApplication[]>; total: number };
  question_sets: QuestionSetItem[];
  model_configured: boolean;
  selection?: {
    ready: boolean;
    code?: string;
    message?: string;
    capacity?: { state: string; code?: string; effective_question_limit: number; compressed_materials: string[]; extra_calls: number };
    requirements?: {
      resume?: ReadinessRequirement;
      jd?: ReadinessRequirement;
    };
  };
};
