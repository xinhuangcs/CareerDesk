export type ReplayReviewPresentation = {
  strengths: string[];
  gaps: string[];
  nextStep: string | null;
  guideText: string | null;
};

function text(value: unknown): string | null {
  return typeof value === "string" && value.trim() ? value.trim() : null;
}

function textList(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value.flatMap((item) => {
    const content = text(item);
    return content ? [content] : [];
  });
}

export function presentReplayReview(
  feedback: Record<string, unknown>,
  answerGuide: Record<string, unknown>,
): ReplayReviewPresentation {
  return {
    strengths: textList(feedback.strengths),
    gaps: textList(feedback.gaps),
    nextStep: text(feedback.next_step),
    guideText: text(answerGuide.text),
  };
}
