import { getJson, postJson, putJson } from "../../shared/api/transport.ts";
import { currentOutputLocale } from "../../i18n/localePreference.ts";
import {
  parseResumeAdaptationInputPreview,
  parseResumeAdaptationResponse,
  parseResumeBindingResponse,
  type ResumeAdaptationGenerateRequest,
  type ResumeAdaptationInputPreview,
  type ResumeAdaptationResponse,
  type ResumeBindingRequest,
  type ResumeBindingResponse,
} from "./resumeAdaptationContract.ts";

function resource(applicationId: number, suffix: string): string {
  if (!Number.isInteger(applicationId) || applicationId <= 0) {
    throw new TypeError("岗位编号无效");
  }
  return `/api/timeline/applications/${applicationId}/${suffix}`;
}

export async function getResumeAdaptation(
  applicationId: number,
  options: { signal?: AbortSignal } = {},
): Promise<ResumeAdaptationResponse> {
  const locale = encodeURIComponent(currentOutputLocale());
  const payload = await getJson<unknown>(`${resource(applicationId, "resume-adaptation")}?locale=${locale}`, {
    cache: "no-store",
    signal: options.signal,
  });
  return parseResumeAdaptationResponse(payload);
}

export async function generateResumeAdaptation(
  applicationId: number,
  request: ResumeAdaptationGenerateRequest,
  options: { signal?: AbortSignal } = {},
): Promise<ResumeAdaptationResponse> {
  const payload = await postJson<unknown>(
    resource(applicationId, "resume-adaptation"),
    {
      refresh: request.refresh,
      expected_resume_id: request.expected_resume_id ?? null,
      accept_no_research: request.accept_no_research ?? false,
      accept_summarized: request.accept_summarized ?? false,
      output_locale: currentOutputLocale(),
    },
    { signal: options.signal },
  );
  return parseResumeAdaptationResponse(payload);
}

export async function bindApplicationResume(
  applicationId: number,
  request: ResumeBindingRequest,
): Promise<ResumeBindingResponse> {
  const payload = await putJson<unknown>(
    resource(applicationId, "resume-binding"),
    request,
  );
  return parseResumeBindingResponse(payload);
}

export async function getResumeAdaptationInputPreview(
  applicationId: number,
  options: { signal?: AbortSignal } = {},
): Promise<ResumeAdaptationInputPreview> {
  const payload = await getJson<unknown>(
    `${resource(applicationId, "resume-adaptation/input-preview")}?locale=${encodeURIComponent(currentOutputLocale())}`,
    { cache: "no-store", signal: options.signal },
  );
  return parseResumeAdaptationInputPreview(payload);
}
