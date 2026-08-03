import { del, getJson, postJson } from "../../shared/api/transport.ts";
import {
  parseDeleteSetResponse, parseGrillFlowResponse, parseMutationResponse,
  parseReadinessResponse, parseReplayResponse, parseSessionsResponse,
  parseStatusResponse,
} from "./grillContract.ts";
import type { GrillFlowResponse, ReadinessResponse, SessionListItem, SessionReplay } from "./grillContract.ts";

export async function getReadiness(query = ""): Promise<ReadinessResponse> {
  return parseReadinessResponse(await getJson<unknown>(`/api/interview-generation/readiness${query ? `?${query}` : ""}`));
}

export function generateSet(body: Record<string, unknown>): Promise<{ status: string; question_set_id?: number; code?: string; message?: string }> {
  return postJson<unknown>("/api/interview-generation/question-sets", body).then(parseStatusResponse);
}

export function deleteQuestionSet(id: number): Promise<{ status: "deleted" | "archived" | "not_found" }> {
  return del<unknown>(`/api/interview-generation/question-sets/${id}`).then(parseDeleteSetResponse);
}

export function claimGrillExperimentIntro(): Promise<{ should_show: boolean; release_version: string }> {
  return postJson<{ should_show: boolean; release_version: string }>("/api/grill/experiment-intro/claim", {});
}

export async function getGrillSessions(state: "active,suspended" | "finished"): Promise<SessionListItem[]> {
  return parseSessionsResponse(await getJson<unknown>(`/api/grill/sessions?state=${state}`));
}

export function startGrill(questionSetId: number, questionCount: number): Promise<GrillFlowResponse> {
  return postJson<unknown>("/api/grill/start", { question_set_id: questionSetId, question_count: questionCount }).then(parseGrillFlowResponse);
}

export function resumeGrill(sessionId: number): Promise<GrillFlowResponse> {
  return postJson<unknown>("/api/grill/resume", { session_id: sessionId }).then(parseGrillFlowResponse);
}

export function answerGrill(sessionId: number, sessionItemId: number, text: string, answeringFollowUp: boolean): Promise<GrillFlowResponse> {
  return postJson<unknown>("/api/grill/answer", { session_id: sessionId, session_item_id: sessionItemId, text, answering_follow_up: answeringFollowUp }).then(parseGrillFlowResponse);
}

export function skipGrill(sessionId: number, sessionItemId: number): Promise<GrillFlowResponse> {
  return postJson<unknown>("/api/grill/skip", { session_id: sessionId, session_item_id: sessionItemId }).then(parseGrillFlowResponse);
}

export function suspendGrill(sessionId: number): Promise<GrillFlowResponse> {
  return postJson<unknown>("/api/grill/suspend", { session_id: sessionId }).then(parseGrillFlowResponse);
}

export function finalizeGrillSession(sessionId: number): Promise<SessionReplay> {
  return postJson<unknown>(`/api/grill/sessions/${sessionId}/finalize`, {}).then(parseReplayResponse);
}

export function getGrillSessionSummary(sessionId: number): Promise<SessionReplay> {
  return getJson<unknown>(`/api/grill/sessions/${sessionId}/summary`).then(parseReplayResponse);
}

export function deleteGrillSession(sessionId: number): Promise<{ status: string; message?: string }> {
  return del<unknown>(`/api/grill/sessions/${sessionId}`).then(parseMutationResponse);
}
