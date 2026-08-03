import type { QuestionSource } from "./questionSource";

export type QuestionQualityFlag = "good" | "bad" | null;

/**
 * Question rewrites are safe only for generated questions that the user has
 * explicitly marked as needing improvement. Real and imported questions may
 * still be quality-rated, but their original wording must remain untouched.
 */
export function canOpenQuestionImprovement(
  source: QuestionSource,
  qualityFlag: QuestionQualityFlag,
): boolean {
  return source === "generated" && qualityFlag === "bad";
}
