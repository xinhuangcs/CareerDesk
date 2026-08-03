import type { ReactNode } from "react";

type ProposalDecisionTone = "info" | "danger";

const TONE_STYLES: Record<ProposalDecisionTone, {
  card: string;
  header: string;
  dot: string;
  description: string;
}> = {
  info: {
    card: "border-info/35",
    header: "bg-info-soft",
    dot: "bg-info",
    description: "text-info",
  },
  danger: {
    card: "border-bad/35",
    header: "bg-bad-soft",
    dot: "bg-bad",
    description: "text-bad",
  },
};

export function ProposalDecisionCard({
  id,
  tone,
  title,
  description,
  timeLabel,
  actions,
  children,
  supplement,
  error,
  dimmed = false,
}: {
  id: string;
  tone: ProposalDecisionTone;
  title: ReactNode;
  description: ReactNode;
  timeLabel: string;
  actions: ReactNode;
  children: ReactNode;
  supplement?: ReactNode;
  error?: string | null;
  dimmed?: boolean;
}) {
  const styles = TONE_STYLES[tone];
  const titleId = `${id}-title`;

  return (
    <section
      id={id}
      aria-labelledby={titleId}
      className={`card scroll-mt-20 overflow-hidden transition-opacity ${styles.card} ${dimmed ? "opacity-55" : ""}`}
    >
      <div className={`border-b border-line px-4 py-3 ${styles.header}`}>
        <div className="flex flex-col gap-3 sm:flex-row sm:items-start sm:justify-between">
          <div className="flex min-w-0 items-start gap-2">
            <span className={`mt-1.5 h-2 w-2 shrink-0 rounded-full ${styles.dot}`} aria-hidden />
            <h2 id={titleId} className="min-w-0 break-words text-sm font-semibold">
              {title}
            </h2>
          </div>
          <div className="flex shrink-0 flex-wrap items-center gap-2 sm:justify-end">
            {actions}
          </div>
        </div>
        <p className={`mt-2 text-xs leading-relaxed ${styles.description}`}>
          {description}
        </p>
        <p className="mt-2 text-xs tabular-nums text-ink-3">{timeLabel}</p>
      </div>
      <div className="p-4">
        <div className="rounded-xl bg-panel-2 px-3.5 py-3 text-xs text-ink-2">
          {children}
        </div>
        {supplement}
      </div>
      {error && (
        <p role="alert" className="border-t border-line bg-bad-soft px-4 py-2.5 text-xs text-bad">
          {error}
        </p>
      )}
    </section>
  );
}
