import type {
  ResumeAdaptationResponse,
  ResumeAdaptationState,
  ResumeSectionReview,
} from "./resumeAdaptationContract.ts";
import { formatDate } from "../../i18n/formatters.ts";
import type { UiLocale } from "../../i18n/i18n.ts";

export type ResumeAdaptationPrimaryAction =
  | "generate"
  | "choose_resume"
  | "upload_resume"
  | "reupload_resume"
  | "edit_jd"
  | "run_research"
  | "configure_research"
  | "continue_without_research"
  | "configure_model"
  | "confirm_summarized"
  | "retry"
  | "reload";

export type ResumeAdaptationStateView = {
  tone: "neutral" | "info" | "warn" | "bad";
  title: string;
  description: string;
  primary_action: ResumeAdaptationPrimaryAction | null;
  adaptation_button_visible: boolean;
};

function unreachable(value: never): never {
  throw new TypeError(`Unknown resume adaptation state: ${String(value)}`);
}

function copy(locale: UiLocale, zhCN: string, en: string): string {
  return locale === "en" ? en : zhCN;
}

/**
 * The response state is the only authority for presentation. Server messages are optional
 * supplemental text and deliberately do not participate in this mapping.
 */
export function resumeAdaptationStateView(
  response: ResumeAdaptationResponse,
  locale: UiLocale = "zh-CN",
): ResumeAdaptationStateView {
  const state: ResumeAdaptationState = response.state;
  switch (state) {
    case "ready":
      return {
        tone: "neutral",
        title: copy(locale, "材料已就绪", "Materials ready"),
        description: copy(locale, "生成前可核对当前简历版本、将发送给模型的文本和数据发送范围。", "Before generating, verify the résumé version, model-input text, and data-sharing scope."),
        primary_action: "generate",
        adaptation_button_visible: true,
      };
    case "generation_running":
      return {
        tone: "info",
        title: copy(locale, "正在生成简历优化建议", "Generating résumé recommendations"),
        description: copy(locale, "任务仍在后台运行，离开本页不会中断；返回后会继续显示进度并自动更新结果。", "The task continues in the background if you leave; returning restores progress and updates the result automatically."),
        primary_action: null,
        adaptation_button_visible: false,
      };
    case "ok":
      return {
        tone: "neutral",
        title: copy(locale, "简历优化建议", "Résumé recommendations"),
        description: response.cached ? copy(locale, "已复用当前材料的有效报告。", "Reused the valid report for the current materials.") : copy(locale, "报告已生成并通过校验。", "The report was generated and validated."),
        primary_action: null,
        adaptation_button_visible: false,
      };
    case "no_resume":
      return {
        tone: "neutral",
        title: copy(locale, "尚未上传简历", "No résumé uploaded"),
        description: copy(locale, "先到资料库上传一份可读取文字的简历，再回到这个岗位。", "Upload a résumé with extractable text in the Library, then return to this role."),
        primary_action: "upload_resume",
        adaptation_button_visible: false,
      };
    case "resume_selection_required":
      return {
        tone: "neutral",
        title: copy(locale, "确认本次使用的简历版本", "Confirm the résumé version"),
        description: copy(locale, "选择会先保存到岗位；刷新或重新打开后不会丢失。", "The selection is saved to this role and persists across refreshes."),
        primary_action: "choose_resume",
        adaptation_button_visible: false,
      };
    case "resume_reupload_required":
      return {
        tone: "warn",
        title: copy(locale, "这份简历的文字无法可靠读取", "This résumé's text cannot be read reliably"),
        description: copy(locale, "请重新上传可复制文字的 PDF、DOCX、Markdown 或 TXT 文件。", "Upload a PDF, DOCX, Markdown, or TXT file with selectable text."),
        primary_action: "reupload_resume",
        adaptation_button_visible: false,
      };
    case "missing_jd":
      return {
        tone: "warn",
        title: copy(locale, "岗位描述还不完整", "The job description is incomplete"),
        description: copy(locale, "补充完整 JD 后再生成，系统不会只按岗位名称猜测要求。", "Add the complete job description before generating; requirements are never guessed from the title alone."),
        primary_action: "edit_jd",
        adaptation_button_visible: false,
      };
    case "research_required":
      return {
        tone: "info",
        title: response.research?.artifact_state === "stale" ? copy(locale, "公司调研需要刷新", "Company research needs refreshing") : copy(locale, "需要先生成公司调研", "Company research required"),
        description: copy(locale, "调研形成当前岗位的一致资料后，才能生成简历优化建议。", "Generate consistent research for this role before creating résumé recommendations."),
        primary_action: "run_research",
        adaptation_button_visible: false,
      };
    case "research_running":
      return {
        tone: "info",
        title: copy(locale, "公司调研正在进行", "Company research in progress"),
        description: copy(locale, "当前正在生成调研；简历优化尚未开始。", "Research is being generated; résumé optimization has not started."),
        primary_action: null,
        adaptation_button_visible: false,
      };
    case "research_failed":
      return {
        tone: "bad",
        title: copy(locale, "公司调研未完成", "Company research did not complete"),
        description: copy(locale, "报告不会绕过仍可用的调研能力；请核对原因后重试。", "The report will not bypass available research capability. Check the cause and retry."),
        primary_action: "run_research",
        adaptation_button_visible: false,
      };
    case "research_disabled":
      return {
        tone: "warn",
        title: copy(locale, "公司调研已停用", "Company research is disabled"),
        description: copy(locale, "可以调整调研设置，或明确确认本次只按 JD 与简历生成。", "Adjust research settings or explicitly confirm generation from only the job description and résumé."),
        primary_action: response.no_research_fallback_available
          ? "continue_without_research"
          : "configure_research",
        adaptation_button_visible: response.no_research_fallback_available,
      };
    case "research_unavailable":
      return {
        tone: "warn",
        title: copy(locale, "公司调研当前不可用", "Company research is unavailable"),
        description: copy(locale, "请检查模型、网络和调研配置；也可以明确确认本次不含公司背景。", "Check model, network, and research settings, or explicitly confirm generation without company context."),
        primary_action: response.no_research_fallback_available
          ? "continue_without_research"
          : "configure_research",
        adaptation_button_visible: response.no_research_fallback_available,
      };
    case "model_required":
      return {
        tone: "warn",
        title: copy(locale, "需要先配置模型", "Configure a model first"),
        description: copy(locale, "已有有效报告不会受影响；新生成需要可用模型。", "Existing valid reports are unaffected; new generation needs an available model."),
        primary_action: "configure_model",
        adaptation_button_visible: false,
      };
    case "insufficient_model_capacity":
      return {
        tone: "warn",
        title: copy(locale, "当前模型容量不足", "Current model capacity is insufficient"),
        description: response.summarization_available
          ? copy(locale, "可知情确认使用压缩摘要；这会额外调用模型，超长文件可能分块多次，且没有逐段点评。", "You can confirm use of a compressed summary. This adds model calls, may chunk very long files, and omits line-by-line commentary.")
          : copy(locale, "请改用更大上下文模型，或上传正常长度、文字可读取的简历。", "Use a model with a larger context window or upload a normal-length résumé with extractable text."),
        primary_action: response.summarization_available
          ? "confirm_summarized"
          : "configure_model",
        adaptation_button_visible: response.summarization_available,
      };
    case "invalid_model_output":
      return {
        tone: "bad",
        title: copy(locale, "模型结果未通过校验", "Model output failed validation"),
        description: copy(locale, "不完整或引用不合法的内容没有发布，可以明确重试。", "Incomplete or invalidly referenced content was not published. You can retry explicitly."),
        primary_action: "retry",
        adaptation_button_visible: true,
      };
    case "stale":
      return {
        tone: "warn",
        title: copy(locale, "生成期间材料发生变化", "Materials changed during generation"),
        description: copy(locale, "旧结果没有发布；重新读取当前材料后再生成。", "The stale result was not published. Reload current materials before generating again."),
        primary_action: "reload",
        adaptation_button_visible: false,
      };
    case "provider_error":
      return {
        tone: "bad",
        title: copy(locale, "模型调用未完成", "Model call did not complete"),
        description: copy(locale, "本次没有发布报告；检查模型服务后可以重试。", "No report was published. Check the model service and retry."),
        primary_action: "retry",
        adaptation_button_visible: true,
      };
    default:
      return unreachable(state);
  }
}

const OPEN_PRIORITY: Partial<Record<ResumeSectionReview["assessment"], number>> = {
  highly_aligned: 0,
  needs_work: 1,
  aligned: 2,
};

/** Pick open section ids by priority without changing the report's resume order. */
export function defaultExpandedSectionIds(
  sections: readonly ResumeSectionReview[],
  limit = 5,
): Set<string> {
  if (!Number.isInteger(limit) || limit < 0) throw new TypeError("Expanded section count must be a non-negative integer");
  return new Set(
    sections
      .map((section, index) => ({ section, index, rank: OPEN_PRIORITY[section.assessment] }))
      .filter((item): item is typeof item & { rank: number } => item.rank !== undefined)
      .sort((left, right) => left.rank - right.rank || left.index - right.index)
      .slice(0, limit)
      .map(({ section }) => section.section_id),
  );
}

export function formatAdaptationElapsed(seconds: number, locale: UiLocale = "zh-CN"): string {
  const safeSeconds = Number.isFinite(seconds) ? Math.max(0, Math.floor(seconds)) : 0;
  const minutes = Math.floor(safeSeconds / 60);
  const remainder = safeSeconds % 60;
  if (locale === "en") return minutes > 0 ? `${minutes}m ${remainder.toString().padStart(2, "0")}s` : `${remainder}s`;
  return minutes > 0 ? `${minutes}分${remainder.toString().padStart(2, "0")}秒` : `${remainder}秒`;
}

/** Render trusted ISO timestamps in the user's local timezone without seconds. */
export function formatAdaptationDateTime(value: string, locale: UiLocale = "zh-CN"): string {
  return formatDate(value, locale, "dateTime") || value;
}
