import { WRITE_HEADERS } from "../../shared/api/headers.ts";

export type TimeZoneDetector = () => string | null;

export function detectSystemTimeZone(): string | null {
  try {
    const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone?.trim();
    return timezone && timezone.length <= 128 ? timezone : null;
  } catch {
    return null;
  }
}

export async function postSystemTimeZone(
  timezone: string,
  signal?: AbortSignal,
): Promise<boolean> {
  try {
    const response = await fetch("/api/settings/system-timezone", {
      method: "POST",
      headers: {
        ...WRITE_HEADERS,
        "Content-Type": "application/json",
      },
      body: JSON.stringify({ timezone }),
      signal,
    });
    return response.ok;
  } catch {
    return false;
  }
}

export async function syncSystemTimeZoneBeforeAppStart(
  detector: TimeZoneDetector = detectSystemTimeZone,
  poster: typeof postSystemTimeZone = postSystemTimeZone,
  timeoutMilliseconds = 2_000,
): Promise<void> {
  const timezone = detector();
  if (!timezone) return;

  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMilliseconds);
  try {
    await poster(timezone, controller.signal);
  } finally {
    window.clearTimeout(timeout);
  }
}

/**
 * Keep the local desktop instance aligned with the operating-system timezone.
 * Detection runs at startup and whenever the window becomes active again, so
 * changing zones while travelling does not require reinstalling CareerDesk.
 */
export function installSystemTimeZoneSync(
  detector: TimeZoneDetector = detectSystemTimeZone,
  poster: typeof postSystemTimeZone = postSystemTimeZone,
): () => void {
  let stopped = false;
  let inFlight = false;
  let queued = false;
  let lastApplied: string | null = null;
  let controller: AbortController | null = null;

  const synchronize = async () => {
    if (stopped) return;
    const timezone = detector();
    if (!timezone || timezone === lastApplied) return;
    if (inFlight) {
      queued = true;
      return;
    }

    inFlight = true;
    controller = new AbortController();
    const applied = await poster(timezone, controller.signal);
    if (applied) lastApplied = timezone;
    inFlight = false;
    controller = null;

    if (queued && !stopped) {
      queued = false;
      void synchronize();
    }
  };

  const onFocus = () => void synchronize();
  const onVisibilityChange = () => {
    if (document.visibilityState === "visible") void synchronize();
  };

  window.addEventListener("focus", onFocus);
  document.addEventListener("visibilitychange", onVisibilityChange);
  void synchronize();

  return () => {
    stopped = true;
    controller?.abort();
    window.removeEventListener("focus", onFocus);
    document.removeEventListener("visibilitychange", onVisibilityChange);
  };
}
