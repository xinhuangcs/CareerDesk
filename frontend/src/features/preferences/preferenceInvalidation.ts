const CHANNEL_NAME = "careerdesk_preferences_v1";
const SCOPE_PATTERN = /^[0-9a-f]{64}$/;

type InvalidationMessage = {
  version: 1;
  type: "invalidate";
  recovery_scope: string;
};

function isInvalidationMessage(value: unknown, scope: string): value is InvalidationMessage {
  if (value === null || typeof value !== "object" || Array.isArray(value)) return false;
  const record = value as Record<string, unknown>;
  const keys = Object.keys(record);
  return keys.length === 3
    && keys.includes("version")
    && keys.includes("type")
    && keys.includes("recovery_scope")
    && record.version === 1
    && record.type === "invalidate"
    && record.recovery_scope === scope
    && SCOPE_PATTERN.test(scope);
}

export function subscribePreferenceInvalidation(
  recoveryScope: string,
  onInvalidate: () => void,
): () => void {
  if (!SCOPE_PATTERN.test(recoveryScope) || typeof BroadcastChannel === "undefined") {
    return () => undefined;
  }
  try {
    const channel = new BroadcastChannel(CHANNEL_NAME);
    const onMessage = (event: MessageEvent<unknown>) => {
      if (isInvalidationMessage(event.data, recoveryScope)) onInvalidate();
    };
    channel.addEventListener("message", onMessage);
    return () => {
      try {
        channel.removeEventListener("message", onMessage);
        channel.close();
      } catch {
        // focus/visibility revalidation remains the fallback.
      }
    };
  } catch {
    return () => undefined;
  }
}

export function broadcastPreferenceInvalidation(recoveryScope: string): boolean {
  if (!SCOPE_PATTERN.test(recoveryScope) || typeof BroadcastChannel === "undefined") return false;
  let channel: BroadcastChannel | null = null;
  try {
    channel = new BroadcastChannel(CHANNEL_NAME);
    channel.postMessage({
      version: 1,
      type: "invalidate",
      recovery_scope: recoveryScope,
    } satisfies InvalidationMessage);
    return true;
  } catch {
    return false;
  } finally {
    channel?.close();
  }
}
