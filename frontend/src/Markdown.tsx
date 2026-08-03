import { lazy, Suspense } from "react";

const MarkdownRenderer = lazy(() => import("./MarkdownRenderer"));

/** Load the sizeable Markdown parser only when formatted content is actually visible. */
export function Markdown({ text }: { text: string }) {
  return (
    <Suspense fallback={<div className="whitespace-pre-wrap text-sm leading-relaxed">{text}</div>}>
      <MarkdownRenderer text={text} />
    </Suspense>
  );
}
