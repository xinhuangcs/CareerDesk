import type { ResumeAdaptationResponse } from "./resumeAdaptationContract.ts";

type TimerHandle = ReturnType<typeof globalThis.setTimeout>;

type PollClock = {
  setTimeout: (callback: () => void, delayMs: number) => TimerHandle;
  clearTimeout: (handle: TimerHandle) => void;
};

type ResearchPollOptions = {
  intervalMs: number;
  read: () => Promise<ResumeAdaptationResponse | null>;
  onReady: (response: ResumeAdaptationResponse) => void | Promise<void>;
  onTerminal: (response: ResumeAdaptationResponse) => void;
  clock?: PollClock;
};

const defaultClock: PollClock = {
  setTimeout: (callback, delayMs) => globalThis.setTimeout(callback, delayMs),
  clearTimeout: (handle) => globalThis.clearTimeout(handle),
};

/**
 * Poll research serially until it becomes ready or reaches another terminal state.
 * A failed read is transient: the next check is scheduled after the current read
 * settles, so slow requests never overlap or continually abort one another.
 */
export function startResumeAdaptationResearchPolling({
  intervalMs,
  read,
  onReady,
  onTerminal,
  clock = defaultClock,
}: ResearchPollOptions): () => void {
  let cancelled = false;
  let timer: TimerHandle | null = null;

  const schedule = () => {
    if (cancelled) return;
    timer = clock.setTimeout(() => {
      timer = null;
      void tick();
    }, intervalMs);
  };

  const tick = async () => {
    if (cancelled) return;
    let next: ResumeAdaptationResponse | null = null;
    try {
      next = await read();
    } catch {
      // The panel read path reports its own sanitized error. Treat an unexpected
      // rejection as transient as well so one network failure cannot stop follow-up.
    }
    if (cancelled) return;
    if (next === null) {
      schedule();
      return;
    }
    if (next.state === "ready") {
      cancelled = true;
      await onReady(next);
      return;
    }
    if (next.state === "research_running"
        || next.state === "research_required"
        || next.state === "generation_running") {
      schedule();
      return;
    }
    cancelled = true;
    onTerminal(next);
  };

  schedule();
  return () => {
    cancelled = true;
    if (timer !== null) clock.clearTimeout(timer);
    timer = null;
  };
}
