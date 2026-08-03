import { WRITE_HEADERS } from "../../shared/api/headers.ts";
import type {
  PreferenceItemCommandPayload,
  PreferenceItemCommandSkeleton,
} from "./preferenceItemCommandContract.ts";

export function preferencesReadRequest(
  signal: AbortSignal,
): { url: string; init: Pick<RequestInit, "cache" | "signal"> } {
  return {
    url: "/api/preferences",
    init: { cache: "no-store", signal },
  };
}

function itemCommandUrl(commandId: string): string {
  return `/api/preferences/item-commands/${encodeURIComponent(commandId)}`;
}

export function preferenceItemCommandReadRequest(
  commandId: string,
  signal: AbortSignal,
): { url: string; init: RequestInit } {
  return {
    url: itemCommandUrl(commandId),
    init: { cache: "no-store", signal },
  };
}

export function preferenceItemCommandPutRequest(
  commandId: string,
  payload: PreferenceItemCommandPayload,
  signal: AbortSignal,
): { url: string; init: RequestInit } {
  return {
    url: itemCommandUrl(commandId),
    init: {
      method: "PUT",
      cache: "no-store",
      headers: { ...WRITE_HEADERS, "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal,
    },
  };
}

export function preferenceItemCommandCancelRequest(
  commandId: string,
  skeleton: PreferenceItemCommandSkeleton,
  signal: AbortSignal,
): { url: string; init: RequestInit } {
  return {
    url: `${itemCommandUrl(commandId)}/cancel-if-absent`,
    init: {
      method: "POST",
      cache: "no-store",
      headers: { ...WRITE_HEADERS, "Content-Type": "application/json" },
      body: JSON.stringify(skeleton),
      signal,
    },
  };
}
