import { createElement } from "react";

type ChatAssistantProgressProps = {
  busy: boolean;
  messageId: string;
  clientTurnId: string | null;
  label: string | null;
  afterText?: boolean;
};

export function ChatAssistantProgress({
  busy,
  messageId,
  clientTurnId,
  label,
  afterText = false,
}: ChatAssistantProgressProps) {
  if (!busy || clientTurnId === null || messageId !== `${clientTurnId}:assistant` || !label) {
    return null;
  }
  return createElement(
    "span",
    {
      role: "status",
      className: `${afterText ? "mt-2 " : ""}flex items-center gap-1.5 text-sm text-ink-3`,
    },
    createElement("span", {
      "aria-hidden": true,
      className: "inline-block h-3.5 w-3.5 shrink-0 animate-spin rounded-full border-2 border-current border-r-transparent",
    }),
    label,
  );
}
