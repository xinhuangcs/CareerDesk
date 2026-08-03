export const RESUME_ADAPTATION_STATES = [
  "ready",
  "generation_running",
  "ok",
  "no_resume",
  "resume_selection_required",
  "resume_reupload_required",
  "missing_jd",
  "research_required",
  "research_running",
  "research_failed",
  "research_disabled",
  "research_unavailable",
  "model_required",
  "insufficient_model_capacity",
  "invalid_model_output",
  "stale",
  "provider_error",
] as const;

export type ResumeAdaptationState = (typeof RESUME_ADAPTATION_STATES)[number];

export type ResumeAdaptationResearchAction =
  | "start"
  | "restart"
  | "refresh"
  | "retry"
  | null;

export type ResumeExtractionReceipt = {
  status: "usable" | "reupload_required";
  char_count: number;
  non_whitespace_count: number;
  alnum_count: number;
  replacement_char_count: number;
  replacement_ratio: number;
  control_char_count: number;
  control_ratio: number;
  reason_codes: string[];
  warning_codes: string[];
};

export type ResumeAdaptationResume = {
  id: number;
  name: string;
  updated_time: string | null;
  extraction_receipt: ResumeExtractionReceipt | null;
};

export type ResumeAdaptationResearch = {
  artifact_state: "missing" | "ready" | "stale" | "legacy";
  attempt_state:
    | "idle"
    | "pending"
    | "running"
    | "succeeded"
    | "failed"
    | "disabled"
    | "unavailable";
  coverage_quality: "complete" | "partial" | "insufficient" | null;
  fresh_until: string | null;
  error_code: string | null;
  action: ResumeAdaptationResearchAction;
};

export type ResumeAdaptationModelDisclosure = {
  provider: string;
  model: string;
  label: string;
};

export type ResumeAdaptationEnvelope = {
  artifact_version: number;
  resume_id: number;
  resume_name: string;
  resume_selection: "bound" | "confirmed";
  research_mode: "snapshot" | "no_research";
  research_snapshot_id: string | null;
  resume_input_form: "full_text" | "summarized";
  generated_time: string;
  content_locale: "zh-CN" | "en";
};

export type ResumeRequirementAssessment = {
  requirement: string;
  importance: "must" | "preferred" | "context";
  evidence: "strong" | "partial" | "absent" | "uncertain";
  jd_evidence: string[];
  resume_evidence: string[];
  limitation: string;
};

export type ResumeAdaptationAdvice = {
  action: string;
  reason: string;
};

export type ResumeAdaptationRewrite = {
  original_text: string;
  suggested_text: string;
  reason: string;
  verification_needed: boolean;
};

export type ResumeAdaptationEvidence = {
  segment_id: string;
  char_start: number;
  char_end: number;
  text: string;
};

export type ResumeAdaptationSegmentRange = {
  start: ResumeAdaptationEvidence;
  end: ResumeAdaptationEvidence;
};

export type ResumeSectionReview = {
  section_id: string;
  resume_segment_range: ResumeAdaptationSegmentRange;
  title: string;
  assessment: "highly_aligned" | "aligned" | "needs_work" | "keep" | "administrative";
  conclusion: string;
  rationale: string;
  preparation_points: string[];
  improvements: string[];
  rewrites: ResumeAdaptationRewrite[];
};

export type ResumeMajorGap = {
  requirement: string;
  evidence: "partial" | "absent" | "uncertain";
  basis: string;
  jd_evidence: string[];
  resume_evidence: string[];
};

export type ResumeAdaptationReport = {
  mode: "full" | "gap_brief";
  fit_band: "strong" | "promising" | "weak";
  summary_sentences: string[];
  requirement_assessments: ResumeRequirementAssessment[];
  overall_advice: ResumeAdaptationAdvice[];
  section_reviews: ResumeSectionReview[];
  major_gaps: ResumeMajorGap[];
  next_steps: string[];
  analysis_caveats: string[];
};

export type ResumeAdaptationResponse = {
  state: ResumeAdaptationState;
  message: string | null;
  cached: boolean;
  bound_resume: ResumeAdaptationResume | null;
  resume_options: ResumeAdaptationResume[];
  recommended_resume_id: number | null;
  research: ResumeAdaptationResearch | null;
  report: ResumeAdaptationReport | null;
  envelope: ResumeAdaptationEnvelope | null;
  host_limitations: string[];
  analysis_flags: string[];
  estimated_input_tokens: number | null;
  model_disclosure: ResumeAdaptationModelDisclosure | null;
  summarization_available: boolean;
  no_research_fallback_available: boolean;
  model_input_preview_available: boolean;
};

export type ResumeAdaptationGenerateRequest = {
  refresh: boolean;
  expected_resume_id?: number;
  accept_no_research?: boolean;
  accept_summarized?: boolean;
};

export type ResumeBindingRequest = {
  resume_id: number | null;
  expected_edit_revision: number;
};

export type ResumeBindingResponse = {
  resume_id: number | null;
  edit_revision: number;
  bound_resume: ResumeAdaptationResume | null;
};

export type ResumeAdaptationInputPreview = {
  resume_id: number;
  resume_name: string;
  input_form: "full_text" | "summarized";
  text: string;
  host_limitations: string[];
};

const STATES = new Set<string>(RESUME_ADAPTATION_STATES);
const ARTIFACT_STATES = new Set(["missing", "ready", "stale", "legacy"]);
const ATTEMPT_STATES = new Set([
  "idle", "pending", "running", "succeeded", "failed", "disabled", "unavailable",
]);
const COVERAGE_QUALITY = new Set(["complete", "partial", "insufficient"]);
const RESEARCH_ACTIONS = new Set(["start", "restart", "refresh", "retry"]);
const FIT_BANDS = new Set(["strong", "promising", "weak"]);
const IMPORTANCE = new Set(["must", "preferred", "context"]);
const EVIDENCE = new Set(["strong", "partial", "absent", "uncertain"]);
const GAP_EVIDENCE = new Set(["partial", "absent", "uncertain"]);
const SECTION_ASSESSMENTS = new Set([
  "highly_aligned", "aligned", "needs_work", "keep", "administrative",
]);

function isRecord(value: unknown): value is Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value);
}

function fail(): never {
  throw new TypeError("简历优化数据格式异常，请刷新后重试");
}

function exactKeys(record: Record<string, unknown>, expected: readonly string[]): void {
  const keys = Object.keys(record);
  if (keys.length !== expected.length
      || expected.some((key) => !Object.prototype.hasOwnProperty.call(record, key))) fail();
}

function nullableString(value: unknown): string | null {
  if (value === null) return null;
  if (typeof value !== "string") fail();
  return value;
}

function requiredString(value: unknown): string {
  if (typeof value !== "string") fail();
  return value;
}

function requiredBoolean(value: unknown): boolean {
  if (typeof value !== "boolean") fail();
  return value;
}

function nullablePositiveInteger(value: unknown): number | null {
  if (value === null) return null;
  if (!Number.isInteger(value) || (value as number) <= 0) fail();
  return value as number;
}

function nonNegativeInteger(value: unknown): number {
  if (!Number.isInteger(value) || (value as number) < 0) fail();
  return value as number;
}

function stringArray(value: unknown): string[] {
  if (!Array.isArray(value) || !value.every((item) => typeof item === "string")) fail();
  return value;
}

function boundedRatio(value: unknown): number {
  if (typeof value !== "number" || !Number.isFinite(value) || value < 0 || value > 1) fail();
  return value;
}

function parseExtractionReceipt(value: unknown): ResumeExtractionReceipt | null {
  if (value === null) return null;
  if (!isRecord(value)) fail();
  exactKeys(value, [
    "status",
    "char_count",
    "non_whitespace_count",
    "alnum_count",
    "replacement_char_count",
    "replacement_ratio",
    "control_char_count",
    "control_ratio",
    "reason_codes",
    "warning_codes",
  ]);
  if (value.status !== "usable" && value.status !== "reupload_required") fail();
  return {
    status: value.status,
    char_count: nonNegativeInteger(value.char_count),
    non_whitespace_count: nonNegativeInteger(value.non_whitespace_count),
    alnum_count: nonNegativeInteger(value.alnum_count),
    replacement_char_count: nonNegativeInteger(value.replacement_char_count),
    replacement_ratio: boundedRatio(value.replacement_ratio),
    control_char_count: nonNegativeInteger(value.control_char_count),
    control_ratio: boundedRatio(value.control_ratio),
    reason_codes: stringArray(value.reason_codes),
    warning_codes: stringArray(value.warning_codes),
  };
}

function parseResume(value: unknown): ResumeAdaptationResume {
  if (!isRecord(value)) fail();
  exactKeys(value, ["id", "name", "updated_time", "extraction_receipt"]);
  const id = nullablePositiveInteger(value.id);
  if (id === null) fail();
  return {
    id,
    name: requiredString(value.name),
    updated_time: nullableString(value.updated_time),
    extraction_receipt: parseExtractionReceipt(value.extraction_receipt),
  };
}

function parseResumeList(value: unknown): ResumeAdaptationResume[] {
  if (!Array.isArray(value)) fail();
  return value.map(parseResume);
}

function parseResearch(value: unknown): ResumeAdaptationResearch | null {
  if (value === null) return null;
  if (!isRecord(value)
      || typeof value.artifact_state !== "string"
      || !ARTIFACT_STATES.has(value.artifact_state)
      || typeof value.attempt_state !== "string"
      || !ATTEMPT_STATES.has(value.attempt_state)) fail();
  exactKeys(value, [
    "artifact_state",
    "attempt_state",
    "coverage_quality",
    "fresh_until",
    "error_code",
    "action",
  ]);
  const coverage = value.coverage_quality;
  if (coverage !== null
      && (typeof coverage !== "string" || !COVERAGE_QUALITY.has(coverage))) fail();
  const action = value.action;
  if (action !== null
      && (typeof action !== "string" || !RESEARCH_ACTIONS.has(action))) fail();
  return {
    artifact_state: value.artifact_state as ResumeAdaptationResearch["artifact_state"],
    attempt_state: value.attempt_state as ResumeAdaptationResearch["attempt_state"],
    coverage_quality: coverage as ResumeAdaptationResearch["coverage_quality"],
    fresh_until: nullableString(value.fresh_until),
    error_code: nullableString(value.error_code),
    action: action as ResumeAdaptationResearchAction,
  };
}

function parseModelDisclosure(value: unknown): ResumeAdaptationModelDisclosure | null {
  if (value === null) return null;
  if (!isRecord(value)) fail();
  exactKeys(value, ["provider", "model", "label"]);
  return {
    provider: requiredString(value.provider),
    model: requiredString(value.model),
    label: requiredString(value.label),
  };
}

function parseEnvelope(value: unknown): ResumeAdaptationEnvelope | null {
  if (value === null) return null;
  if (!isRecord(value)) fail();
  exactKeys(value, [
    "artifact_version",
    "resume_id",
    "resume_name",
    "resume_selection",
    "research_mode",
    "research_snapshot_id",
    "resume_input_form",
    "generated_time",
    "content_locale",
  ]);
  const resumeId = nullablePositiveInteger(value.resume_id);
  if (resumeId === null
      || (value.resume_selection !== "bound" && value.resume_selection !== "confirmed")
      || (value.research_mode !== "snapshot" && value.research_mode !== "no_research")
      || (value.content_locale !== "zh-CN" && value.content_locale !== "en")
      || (value.resume_input_form !== "full_text" && value.resume_input_form !== "summarized")) fail();
  return {
    artifact_version: nonNegativeInteger(value.artifact_version),
    resume_id: resumeId,
    resume_name: requiredString(value.resume_name),
    resume_selection: value.resume_selection,
    research_mode: value.research_mode,
    research_snapshot_id: nullableString(value.research_snapshot_id),
    resume_input_form: value.resume_input_form,
    generated_time: requiredString(value.generated_time),
    content_locale: value.content_locale,
  };
}

const EVIDENCE_ID = /^[JR][1-9][0-9]*-[0-9]{4,}-[0-9a-f]{8}$/;
const SECTION_ID = /^S1-[0-9]{4,}-[0-9a-f]{8}$/;

function parseEvidence(value: unknown): ResumeAdaptationEvidence {
  if (!isRecord(value)) fail();
  exactKeys(value, ["segment_id", "char_start", "char_end", "text"]);
  const segmentId = requiredString(value.segment_id);
  const charStart = nonNegativeInteger(value.char_start);
  const charEnd = nonNegativeInteger(value.char_end);
  if (!EVIDENCE_ID.test(segmentId) || charEnd < charStart) fail();
  return {
    segment_id: segmentId,
    char_start: charStart,
    char_end: charEnd,
    text: requiredString(value.text),
  };
}

function parseEvidenceArray(value: unknown): ResumeAdaptationEvidence[] {
  if (!Array.isArray(value)) fail();
  return value.map(parseEvidence);
}

function parseSegmentRefs(value: unknown, namespace: "J" | "R"): string[] {
  const refs = stringArray(value);
  if (refs.some((ref) => !EVIDENCE_ID.test(ref) || !ref.startsWith(namespace))
      || new Set(refs).size !== refs.length) fail();
  return refs;
}

function requireMatchingEvidence(
  refs: readonly string[],
  evidence: readonly ResumeAdaptationEvidence[],
): void {
  if (refs.length !== evidence.length
      || refs.some((ref, index) => ref !== evidence[index].segment_id)) fail();
}

function parseSegmentRange(value: unknown): ResumeAdaptationSegmentRange {
  if (!isRecord(value)) fail();
  exactKeys(value, ["start", "end"]);
  return { start: parseEvidence(value.start), end: parseEvidence(value.end) };
}

function parseRequirement(value: unknown): ResumeRequirementAssessment {
  if (!isRecord(value)) fail();
  exactKeys(value, [
    "requirement_summary",
    "requirement_kind",
    "evidence_state",
    "jd_segment_refs",
    "resume_segment_refs",
    "limitation",
    "jd_evidence",
    "resume_evidence",
  ]);
  const importance = value.requirement_kind;
  const evidence = value.evidence_state;
  if (typeof importance !== "string" || !IMPORTANCE.has(importance)
      || typeof evidence !== "string" || !EVIDENCE.has(evidence)) fail();
  const jdRefs = parseSegmentRefs(value.jd_segment_refs, "J");
  const resumeRefs = parseSegmentRefs(value.resume_segment_refs, "R");
  const jdEvidence = parseEvidenceArray(value.jd_evidence);
  const resumeEvidence = parseEvidenceArray(value.resume_evidence);
  requireMatchingEvidence(jdRefs, jdEvidence);
  requireMatchingEvidence(resumeRefs, resumeEvidence);
  return {
    requirement: requiredString(value.requirement_summary),
    importance: importance as ResumeRequirementAssessment["importance"],
    evidence: evidence as ResumeRequirementAssessment["evidence"],
    jd_evidence: jdEvidence.map((item) => item.text),
    resume_evidence: resumeEvidence.map((item) => item.text),
    limitation: requiredString(value.limitation),
  };
}

function parseAdvice(value: unknown): ResumeAdaptationAdvice {
  if (!isRecord(value)) fail();
  exactKeys(value, ["action", "reason"]);
  return { action: requiredString(value.action), reason: requiredString(value.reason) };
}

function parseRewrite(value: unknown): ResumeAdaptationRewrite {
  if (!isRecord(value) || typeof value.verification_needed !== "boolean") fail();
  exactKeys(value, [
    "resume_segment_ref",
    "suggestion",
    "reason",
    "verification_needed",
    "original_text",
  ]);
  const resumeSegmentRef = requiredString(value.resume_segment_ref);
  if (!EVIDENCE_ID.test(resumeSegmentRef) || !resumeSegmentRef.startsWith("R")) fail();
  return {
    original_text: requiredString(value.original_text),
    suggested_text: requiredString(value.suggestion),
    reason: requiredString(value.reason),
    verification_needed: value.verification_needed,
  };
}

function parseSection(value: unknown): ResumeSectionReview {
  if (!isRecord(value)
      || typeof value.assessment !== "string"
      || !SECTION_ASSESSMENTS.has(value.assessment)) fail();
  exactKeys(value, [
    "section_name",
    "resume_segment_start_ref",
    "resume_segment_end_ref",
    "assessment",
    "conclusion",
    "reasoning",
    "preparation_points",
    "improvements",
    "rewrites",
    "section_id",
    "resume_segment_range",
  ]);
  if (!Array.isArray(value.rewrites)) fail();
  const sectionId = requiredString(value.section_id);
  if (!SECTION_ID.test(sectionId)) fail();
  const startRef = requiredString(value.resume_segment_start_ref);
  const endRef = requiredString(value.resume_segment_end_ref);
  if (!EVIDENCE_ID.test(startRef) || !startRef.startsWith("R")
      || !EVIDENCE_ID.test(endRef) || !endRef.startsWith("R")) fail();
  const segmentRange = parseSegmentRange(value.resume_segment_range);
  if (segmentRange.start.segment_id !== startRef || segmentRange.end.segment_id !== endRef) fail();
  const section: ResumeSectionReview = {
    section_id: sectionId,
    resume_segment_range: segmentRange,
    title: requiredString(value.section_name),
    assessment: value.assessment as ResumeSectionReview["assessment"],
    conclusion: requiredString(value.conclusion),
    rationale: requiredString(value.reasoning),
    preparation_points: stringArray(value.preparation_points),
    improvements: stringArray(value.improvements),
    rewrites: value.rewrites.map(parseRewrite),
  };
  if (section.preparation_points.length > 3
      || section.improvements.length > 3
      || section.rewrites.length > 3) fail();
  return section;
}

function parseGap(value: unknown): ResumeMajorGap {
  if (!isRecord(value)) fail();
  exactKeys(value, [
    "requirement_summary",
    "evidence_state",
    "jd_segment_refs",
    "resume_segment_refs",
    "basis",
    "jd_evidence",
    "resume_evidence",
  ]);
  const evidence = value.evidence_state;
  if (typeof evidence !== "string" || !GAP_EVIDENCE.has(evidence)) fail();
  const jdRefs = parseSegmentRefs(value.jd_segment_refs, "J");
  const resumeRefs = parseSegmentRefs(value.resume_segment_refs, "R");
  const jdEvidence = parseEvidenceArray(value.jd_evidence);
  const resumeEvidence = parseEvidenceArray(value.resume_evidence);
  requireMatchingEvidence(jdRefs, jdEvidence);
  requireMatchingEvidence(resumeRefs, resumeEvidence);
  return {
    requirement: requiredString(value.requirement_summary),
    evidence: evidence as ResumeMajorGap["evidence"],
    basis: requiredString(value.basis),
    jd_evidence: jdEvidence.map((item) => item.text),
    resume_evidence: resumeEvidence.map((item) => item.text),
  };
}

function parseObjectArray<T>(value: unknown, parser: (item: unknown) => T): T[] {
  if (!Array.isArray(value)) fail();
  return value.map(parser);
}

function parseReport(value: unknown): ResumeAdaptationReport | null {
  if (value === null) return null;
  if (!isRecord(value)
      || (value.mode !== "full" && value.mode !== "gap_brief")
      || typeof value.fit_band !== "string" || !FIT_BANDS.has(value.fit_band)) fail();
  exactKeys(value, [
    "mode",
    "fit_band",
    "summary_sentences",
    "requirement_assessments",
    "overall_advice",
    "section_reviews",
    "major_gaps",
    "next_steps",
    "analysis_caveats",
  ]);
  const report: ResumeAdaptationReport = {
    mode: value.mode,
    fit_band: value.fit_band as ResumeAdaptationReport["fit_band"],
    summary_sentences: stringArray(value.summary_sentences),
    requirement_assessments: parseObjectArray(value.requirement_assessments, parseRequirement),
    overall_advice: parseObjectArray(value.overall_advice, parseAdvice),
    section_reviews: parseObjectArray(value.section_reviews, parseSection),
    major_gaps: parseObjectArray(value.major_gaps, parseGap),
    next_steps: stringArray(value.next_steps),
    analysis_caveats: stringArray(value.analysis_caveats),
  };
  if (report.summary_sentences.length < 1
      || report.summary_sentences.length > 3
      || report.requirement_assessments.length < 1
      || report.analysis_caveats.length > 5
      || (report.mode === "full" && (
        report.fit_band === "weak"
        || report.requirement_assessments.length > 12
        || report.overall_advice.length < 1
        || report.overall_advice.length > 5
        || report.section_reviews.length > 40
        || report.major_gaps.length > 0
        || report.next_steps.length > 0
      ))
      || (report.mode === "gap_brief" && (
        report.fit_band !== "weak"
        || report.requirement_assessments.length > 5
        || report.overall_advice.length > 0
        || report.section_reviews.length > 0
        || report.major_gaps.length < 1
        || report.major_gaps.length > 3
        || report.next_steps.length < 1
        || report.next_steps.length > 3
      ))) fail();
  return report;
}

export function parseResumeAdaptationResponse(value: unknown): ResumeAdaptationResponse {
  if (!isRecord(value) || typeof value.state !== "string" || !STATES.has(value.state)) fail();
  exactKeys(value, [
    "state",
    "message",
    "cached",
    "bound_resume",
    "resume_options",
    "recommended_resume_id",
    "research",
    "report",
    "envelope",
    "host_limitations",
    "analysis_flags",
    "estimated_input_tokens",
    "model_disclosure",
    "summarization_available",
    "no_research_fallback_available",
    "model_input_preview_available",
  ]);
  const boundResume = value.bound_resume === null ? null : parseResume(value.bound_resume);
  const resumeOptions = parseResumeList(value.resume_options);
  if (new Set(resumeOptions.map((resume) => resume.id)).size !== resumeOptions.length) fail();
  const recommendedResumeId = nullablePositiveInteger(value.recommended_resume_id);
  if (recommendedResumeId !== null
      && !resumeOptions.some((resume) => resume.id === recommendedResumeId)) fail();
  const report = parseReport(value.report);
  const envelope = parseEnvelope(value.envelope);
  if (boundResume !== null && !resumeOptions.some((resume) => resume.id === boundResume.id)) fail();
  if (boundResume !== null && recommendedResumeId !== null) fail();
  if (value.state === "ok" && (
    report === null
    || envelope === null
    || boundResume === null
    || envelope.resume_id !== boundResume.id
    || envelope.resume_name !== boundResume.name
  )) fail();
  if (value.state !== "ok" && (report !== null || envelope !== null)) fail();
  if (value.state === "no_resume" && (
    boundResume !== null || resumeOptions.length > 0 || recommendedResumeId !== null
  )) fail();
  if (value.state === "resume_selection_required" && (
    boundResume !== null || resumeOptions.length === 0 || recommendedResumeId === null
  )) fail();
  return {
    state: value.state as ResumeAdaptationState,
    message: nullableString(value.message),
    cached: requiredBoolean(value.cached),
    bound_resume: boundResume,
    resume_options: resumeOptions,
    recommended_resume_id: recommendedResumeId,
    research: parseResearch(value.research),
    report,
    envelope,
    host_limitations: stringArray(value.host_limitations),
    analysis_flags: stringArray(value.analysis_flags),
    estimated_input_tokens: nullablePositiveInteger(value.estimated_input_tokens),
    model_disclosure: parseModelDisclosure(value.model_disclosure),
    summarization_available: requiredBoolean(value.summarization_available),
    no_research_fallback_available: requiredBoolean(value.no_research_fallback_available),
    model_input_preview_available: requiredBoolean(value.model_input_preview_available),
  };
}

export function parseResumeBindingResponse(value: unknown): ResumeBindingResponse {
  if (!isRecord(value)) fail();
  exactKeys(value, ["resume_id", "edit_revision", "bound_resume"]);
  const resumeId = nullablePositiveInteger(value.resume_id);
  const editRevision = value.edit_revision;
  if (!Number.isInteger(editRevision) || (editRevision as number) < 0) fail();
  const boundResume = value.bound_resume === null ? null : parseResume(value.bound_resume);
  if ((resumeId === null) !== (boundResume === null)
      || (boundResume !== null && boundResume.id !== resumeId)) fail();
  return { resume_id: resumeId, edit_revision: editRevision as number, bound_resume: boundResume };
}

export function parseResumeAdaptationInputPreview(value: unknown): ResumeAdaptationInputPreview {
  if (!isRecord(value)
      || (value.input_form !== "full_text" && value.input_form !== "summarized")) fail();
  exactKeys(value, ["resume_id", "resume_name", "input_form", "text", "host_limitations"]);
  const resumeId = nullablePositiveInteger(value.resume_id);
  if (resumeId === null) fail();
  return {
    resume_id: resumeId,
    resume_name: requiredString(value.resume_name),
    input_form: value.input_form,
    text: requiredString(value.text),
    host_limitations: stringArray(value.host_limitations),
  };
}
