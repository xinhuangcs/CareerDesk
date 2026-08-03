import type { UiLocale } from "../../i18n/i18n";

const GENERATION_ERROR: Record<string, readonly [string, string]> = {
  model_required: ["请先配置模型", "Configure a model first"],
  model_capacity_required: ["当前模型缺少可信容量信息", "The model is missing reliable capacity information"],
  insufficient_model_capacity: ["当前模型上下文不足，请更换模型或精简材料", "The model cannot fit this material. Change models or shorten the input."],
  model_timeout: ["模型生成超过 5 分钟仍未完成，请重试或更换响应更快的模型", "Generation took more than five minutes. Retry or choose a faster model."],
  model_request_failed: ["模型请求失败，请检查模型配置后重试", "The model request failed. Check model settings and retry."],
  invalid_model_output: ["模型返回格式无效，请重试", "The model returned an invalid format. Retry."],
  invalid_summary_ref: ["材料摘要引用无效，请重试", "The source summary contained an invalid reference. Retry."],
  invalid_evidence_ref: ["模型未能根据材料生成可靠题目，请重试", "The model could not create reliable questions from the material. Retry."],
  undeclared_research_ref: ["生成结果错误引用了未授权调研，请重试", "The result referenced research that was not authorized. Retry."],
  question_limit_exceeded: ["模型返回题目数超过本次上限，请重试", "The model returned more questions than allowed. Retry."],
  no_supported_questions: ["当前材料未生成可安全使用的题目，请补充材料后重试", "The material did not produce any safely supported questions. Add more detail and retry."],
  insufficient_supported_questions: ["当前材料未生成可安全使用的题目，请补充材料后重试", "The material did not produce enough safely supported questions. Add more detail and retry."],
  input_changed: ["生成期间材料已变化，请按最新材料重试", "The source material changed during generation. Retry with the latest version."],
  publication_conflict: ["题集发布发生并发冲突，请重试", "A concurrent update prevented publication. Retry."],
  outcome_unknown: ["生成结果无法确认，请核对后重试", "The generation outcome could not be confirmed. Review and retry."],
  unexpected_generation_error: ["题集生成发生意外错误，请重试", "Question-set generation failed unexpectedly. Retry."],
  unsupported_category: ["旧任务包含通用练习不支持的题目类别，请重新生成", "This older task contains a category not supported by general practice. Generate a new set."],
};

export function generationErrorMessage(
  code: string | null | undefined,
  fallback?: string,
  locale: UiLocale = "zh-CN",
) {
  const safeFallback = fallback && fallback !== code && !/^[a-z0-9_]+$/i.test(fallback)
    ? fallback
    : undefined;
  const known = code ? GENERATION_ERROR[code] : undefined;
  if (known) return known[locale === "en" ? 1 : 0];
  return (locale === "zh-CN" ? safeFallback : undefined)
    ?? (locale === "en" ? "Question-set generation failed. Retry." : "题集生成失败，请重试");
}

const READINESS_ERROR: Record<string, readonly [string, string]> = {
  resume_selection_required: ["请选择一份简历", "Select a résumé"],
  no_resume: ["当前选择没有可用简历", "No usable résumé is available for this selection"],
  resume_reupload_required: ["简历正文不可用，请重新上传", "The résumé text is unavailable. Upload it again."],
  application_selection_required: ["请选择一个岗位", "Select a role"],
  missing_jd: ["当前岗位缺少岗位描述，请先前往岗位详情补充", "This role has no job description. Add it in role details first."],
};

export function readinessErrorMessage(
  code: string | null | undefined,
  locale: UiLocale = "zh-CN",
): string {
  const known = code ? READINESS_ERROR[code] : undefined;
  if (known) return known[locale === "en" ? 1 : 0];
  return locale === "en"
    ? "The selected source material is not ready. Review the requirements above."
    : "所选材料尚未准备好，请核对上方要求。";
}
