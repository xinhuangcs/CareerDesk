export type PreferenceOperationReadRequest = {
  url: string;
  init: Pick<RequestInit, "cache" | "signal">;
};

export function preferenceOperationsByTurnReadRequest(
  clientTurnId: string,
  signal: AbortSignal,
): PreferenceOperationReadRequest {
  return {
    url: `/api/preferences/operations/by-client-turn/${encodeURIComponent(clientTurnId)}`,
    init: { cache: "no-store", signal },
  };
}
