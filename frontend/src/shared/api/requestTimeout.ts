import {
  localizeRuntimeMessage,
  type LocalizedRuntimeMessage,
} from "./runtimeLocale.ts";

const REQUEST_TIMEOUT_MS = 12_000;

export async function withRequestTimeout<T>(
  request: (signal: AbortSignal) => Promise<T>,
  externalSignal: AbortSignal | null | undefined,
  timeoutMessage: string | LocalizedRuntimeMessage,
): Promise<T> {
  const controller = new AbortController();
  let timedOut = false;
  const abortFromCaller = () => controller.abort(externalSignal?.reason);
  if (externalSignal?.aborted) abortFromCaller();
  else externalSignal?.addEventListener("abort", abortFromCaller, { once: true });
  const timer = globalThis.setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, REQUEST_TIMEOUT_MS);
  try {
    return await request(controller.signal);
  } catch (reason) {
    if (timedOut) {
      throw new Error(localizeRuntimeMessage(timeoutMessage));
    }
    throw reason;
  } finally {
    globalThis.clearTimeout(timer);
    externalSignal?.removeEventListener("abort", abortFromCaller);
  }
}
