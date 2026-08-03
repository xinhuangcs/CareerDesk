export type GrillDeepLink = {
  sessionId: number | null;
  applicationId: number | null;
};

function positiveId(value: string | null): number | null {
  if (value === null || !/^\d+$/.test(value)) return null;
  const parsed = Number(value);
  return Number.isSafeInteger(parsed) && parsed > 0 ? parsed : null;
}

export function parseGrillDeepLink(search: string): GrillDeepLink {
  const params = new URLSearchParams(search);
  return {
    sessionId: positiveId(params.get("session")),
    applicationId: params.get("edition") === "custom"
      ? positiveId(params.get("application"))
      : null,
  };
}
