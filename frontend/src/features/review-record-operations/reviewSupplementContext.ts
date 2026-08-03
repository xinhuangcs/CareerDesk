import type { UiLocale } from "../../i18n/i18n.ts";

const UUID_PATTERN = /^[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/;

export const REVIEW_SUPPLEMENT_VISIBLE_PROMPT = "补充信息（可选）：";
export const REVIEW_SUPPLEMENT_VISIBLE_PROMPT_EN = "Additional details (optional):";
export const MAX_CHAT_MESSAGE_CHARS = 50_000;

function visiblePrompt(locale: UiLocale): string {
  return locale === "en"
    ? REVIEW_SUPPLEMENT_VISIBLE_PROMPT_EN
    : REVIEW_SUPPLEMENT_VISIBLE_PROMPT;
}

export function reviewSupplementComposerText(
  current: string,
  locale: UiLocale = "zh-CN",
): string {
  const prompt = visiblePrompt(locale);
  if (current.startsWith(prompt)) return current;
  const body = removeReviewSupplementComposerPrompt(current);
  return body.trim()
    ? `${prompt}\n${body}`
    : prompt;
}

export function removeReviewSupplementComposerPrompt(current: string): string {
  const prompt = [REVIEW_SUPPLEMENT_VISIBLE_PROMPT, REVIEW_SUPPLEMENT_VISIBLE_PROMPT_EN]
    .find((candidate) => current.startsWith(candidate));
  if (!prompt) return current;
  return current.slice(prompt.length).replace(/^\n/, "");
}

export function reviewSupplementRequestFields(
  supplementText: string,
  reviewReference: string,
  locale: UiLocale = "zh-CN",
): { message: string; review_supplement_reference: string } {
  const en = locale === "en";
  if (!UUID_PATTERN.test(reviewReference)) {
    throw new Error(en ? "The review follow-up reference is invalid." : "复盘补充引用无效。");
  }
  const message = supplementText || (en ? "(see attachment)" : "（见附件）");
  // Pydantic/Python counts Unicode code points, whereas JS string.length counts UTF-16
  // code units. Match the server boundary so emoji-heavy but valid text is not rejected.
  if (Array.from(message).length > MAX_CHAT_MESSAGE_CHARS) {
    throw new Error(
      en
        ? `Review follow-up text cannot exceed ${MAX_CHAT_MESSAGE_CHARS.toLocaleString("en")} characters.`
        : `复盘补充正文不能超过 ${MAX_CHAT_MESSAGE_CHARS.toLocaleString("zh-CN")} 个字符。`,
    );
  }
  return {
    message,
    review_supplement_reference: reviewReference,
  };
}
