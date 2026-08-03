export type LibraryDeepLink = {
  returnTo: string | null;
  resumeId: number | null;
};

export function safeLibraryReturnPath(value: string | null): string | null {
  if (!value || !value.startsWith("/") || value.startsWith("//")) return null;
  let parsed: URL;
  try {
    parsed = new URL(value, "https://careerdesk.invalid");
  } catch {
    return null;
  }
  if (parsed.origin !== "https://careerdesk.invalid"
      || parsed.pathname !== "/timeline"
      || parsed.hash !== "") return null;
  return `${parsed.pathname}${parsed.search}`;
}

export function parseLibraryDeepLink(search: string): LibraryDeepLink {
  const params = new URLSearchParams(search);
  const rawResumeId = params.get("resumeId");
  const resumeId = rawResumeId !== null && /^\d+$/.test(rawResumeId)
    ? Number(rawResumeId)
    : null;
  return {
    returnTo: safeLibraryReturnPath(params.get("returnTo")),
    resumeId: resumeId !== null && Number.isSafeInteger(resumeId) && resumeId > 0
      ? resumeId
      : null,
  };
}
