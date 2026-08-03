import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent,
  type ReactNode,
} from "react";
import { HttpError } from "../../shared/api/transport.ts";
import type { UiLocale } from "../../i18n/i18n.ts";
import { useLocale } from "../../i18n/localePreference.ts";
import { useLocalizer } from "../../i18n/useLocalizer.ts";
import { formatNumber } from "../../i18n/formatters.ts";
import {
  bindApplicationResume,
  generateResumeAdaptation,
  getResumeAdaptation,
  getResumeAdaptationInputPreview,
} from "./resumeAdaptationApi.ts";
import type {
  ResumeAdaptationInputPreview,
  ResumeAdaptationModelDisclosure,
  ResumeAdaptationResearch,
  ResumeAdaptationResearchAction,
  ResumeAdaptationResponse,
  ResumeBindingResponse,
  ResumeSectionReview,
} from "./resumeAdaptationContract.ts";
import {
  defaultExpandedSectionIds,
  formatAdaptationDateTime,
  formatAdaptationElapsed,
  resumeAdaptationStateView,
} from "./resumeAdaptationState.ts";
import { startResumeAdaptationResearchPolling } from "./resumeAdaptationResearchPoll.ts";

const RESEARCH_POLL_INTERVAL_MS = 3_000;
const ADAPTATION_WAIT_TIMEOUT_MS = 190_000;

type Confirmation = "no_research" | "summarized" | null;
type BusyState = "binding" | "research" | "generating" | "preview" | null;
export type ResumeAdaptationPanelStatus = "idle" | "running" | "ready";

export type ResumeAdaptationPanelProps = {
  applicationId: number;
  editRevision: number;
  onApplicationChanged?: (binding: ResumeBindingResponse) => void | Promise<void>;
  onEditJd?: () => void;
  onResearchAction?: (
    action: Exclude<ResumeAdaptationResearchAction, null>,
  ) => void | Promise<void>;
  onConfigureResearch?: () => void;
  onConfigureModel?: () => void;
  onStatusChange?: (status: ResumeAdaptationPanelStatus) => void;
};

function isAbortError(error: unknown): boolean {
  return error instanceof DOMException && error.name === "AbortError";
}

function libraryHref(applicationId: number, resumeId?: number | null): string {
  const returnTo = `/timeline?application=${applicationId}&tab=adaptation`;
  const params = new URLSearchParams({ returnTo });
  if (resumeId) params.set("resumeId", String(resumeId));
  return `#/library?${params.toString()}`;
}

function toneClass(tone: ReturnType<typeof resumeAdaptationStateView>["tone"]): string {
  return {
    neutral: "border-line bg-panel-2 text-ink-2",
    info: "border-info/25 bg-info-soft text-info",
    warn: "border-warn/25 bg-warn-soft text-warn",
    bad: "border-bad/25 bg-bad-soft text-bad",
  }[tone];
}

function assessmentLabel(assessment: ResumeSectionReview["assessment"], locale: UiLocale): string {
  const labels = locale === "en" ? {
    highly_aligned: "Highly aligned", aligned: "Aligned", needs_work: "Needs work",
    keep: "Keep", administrative: "Structure",
  } : {
    highly_aligned: "高度契合",
    aligned: "契合",
    needs_work: "需要修改",
    keep: "建议保留",
    administrative: "结构信息",
  };
  return labels[assessment];
}

function evidenceLabel(value: string, locale: UiLocale): string {
  const labels: Record<string, string> = locale === "en" ? {
    strong: "Strong evidence", partial: "Partial evidence",
    absent: "Not shown in current résumé", uncertain: "Needs verification",
  } : {
    strong: "证据充分",
    partial: "部分证据",
    absent: "当前简历未展示",
    uncertain: "需要核对",
  };
  return labels[value] ?? value;
}

function importanceLabel(value: string, locale: UiLocale): string {
  const labels: Record<string, string> = locale === "en"
    ? { must: "Required", preferred: "Preferred", context: "Context" }
    : { must: "硬要求", preferred: "优先项", context: "背景" };
  return labels[value] ?? value;
}

function researchStatusLabel(research: ResumeAdaptationResearch, locale: UiLocale): string {
  const en = locale === "en";
  if (research.coverage_quality === "complete") return en ? "Complete" : "已完成";
  if (research.coverage_quality === "partial") return en ? "Partial" : "部分完成";
  if (research.coverage_quality === "insufficient") return en ? "Insufficient sources" : "材料不足";
  if (research.artifact_state === "ready") return en ? "Ready" : "已就绪";
  if (research.artifact_state === "stale") return en ? "Expired" : "已过期";
  if (research.artifact_state === "legacy") return en ? "Legacy" : "旧版本";
  return en ? "Missing" : "缺失";
}

function BulletList({ items }: { items: readonly string[] }) {
  if (items.length === 0) return null;
  return (
    <ul className="list-disc space-y-1.5 pl-5 text-sm text-ink-2">
      {items.map((item, index) => <li key={`${index}-${item}`}>{item}</li>)}
    </ul>
  );
}

function TrustLimitations({ response }: { response: ResumeAdaptationResponse }) {
  const l = useLocalizer();
  const limitations = [...response.host_limitations, ...response.analysis_flags];
  const caveats = response.report?.analysis_caveats ?? [];
  return (
    <div className="space-y-3">
      {limitations.length > 0 && (
        <section className="rounded-xl border border-warn/25 bg-warn-soft p-3 text-sm">
          <h3 className="font-semibold text-warn">{l("本次分析范围", "Scope of this analysis")}</h3>
          <div className="mt-2"><BulletList items={limitations} /></div>
        </section>
      )}
      {caveats.length > 0 && (
        <details className="rounded-xl border border-line bg-panel-2 px-3 py-2 text-sm">
          <summary className="cursor-pointer font-medium text-ink-2">{l("查看不确定项", "View uncertainties")}</summary>
          <div className="mt-2"><BulletList items={caveats} /></div>
        </details>
      )}
    </div>
  );
}

function Summary({ sentences }: { sentences: readonly string[] }) {
  const l = useLocalizer();
  return (
    <section aria-labelledby="adaptation-summary-title">
      <h3 id="adaptation-summary-title" className="section-label mb-2">{l("优化摘要", "Optimization summary")}</h3>
      <div className="space-y-1.5 text-sm leading-6 text-ink-2">
        {sentences.map((sentence, index) => <p key={`${index}-${sentence}`}>{sentence}</p>)}
      </div>
    </section>
  );
}

function RewriteCard({ rewrite }: { rewrite: ResumeSectionReview["rewrites"][number] }) {
  const l = useLocalizer();
  return (
    <div className="rounded-xl border border-line bg-panel p-3 text-sm">
      <dl className="grid gap-2">
        <div>
          <dt className="text-xs font-semibold text-ink-3">{l("原文（提取自简历）", "Original text (extracted from résumé)")}</dt>
          <dd className="mt-0.5 whitespace-pre-wrap break-words text-ink-3">
            {rewrite.original_text || l("原文暂不可用，请在“发送内容预览”中核对对应位置。", "Original text is unavailable; verify the corresponding section in the model-input preview.")}
          </dd>
        </div>
        <div>
          <dt className="text-xs font-semibold text-ink-3">{l("建议写法（简历原语言）", "Suggested wording (in the résumé's language)")}</dt>
          <dd className="mt-0.5 whitespace-pre-wrap break-words text-ink">{rewrite.suggested_text}</dd>
        </div>
        <div>
          <dt className="text-xs font-semibold text-ink-3">{l("为什么这样改", "Why this change")}</dt>
          <dd className="mt-0.5 whitespace-pre-wrap break-words text-ink-2">{rewrite.reason}</dd>
        </div>
        <div>
          <dt className="text-xs font-semibold text-ink-3">{l("需要你核实/补充的事实（如有）", "Facts to verify or add")}</dt>
          <dd className={`mt-0.5 whitespace-pre-wrap break-words ${rewrite.verification_needed ? "text-warn" : "text-ink-3"}`}>
            {rewrite.verification_needed
              ? l("请核实建议写法中的占位信息与新增事实后再使用。", "Verify placeholders and any newly introduced facts before using this wording.")
              : l("无", "None")}
          </dd>
        </div>
      </dl>
    </div>
  );
}

function FullReport({ response }: { response: ResumeAdaptationResponse }) {
  const l = useLocalizer();
  const { locale } = useLocale();
  const report = response.report;
  if (!report || report.mode !== "full") return null;
  const [expanded, setExpanded] = useState(() => defaultExpandedSectionIds(report.section_reviews));

  function toggle(sectionId: string) {
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(sectionId)) next.delete(sectionId);
      else next.add(sectionId);
      return next;
    });
  }

  return (
    <div className="space-y-5">
      {report.overall_advice.length > 0 && (
        <section aria-labelledby="overall-advice-title">
          <h3 id="overall-advice-title" className="section-label mb-2">{l("整体建议", "Overall advice")}</h3>
          <ol className="space-y-2">
            {report.overall_advice.map((advice, index) => (
              <li key={`${index}-${advice.action}`} className="rounded-xl border border-line bg-panel-2 p-3 text-sm">
                <p className="font-medium text-ink">{index + 1}. {advice.action}</p>
                <p className="mt-1 text-ink-2">{advice.reason}</p>
              </li>
            ))}
          </ol>
        </section>
      )}

      {report.requirement_assessments.length > 0 && (
        <details className="rounded-xl border border-line bg-panel-2 px-3 py-2">
          <summary className="cursor-pointer text-sm font-semibold text-ink">{l("岗位要求依据", "Role-requirement evidence")}</summary>
          <div className="mt-3 space-y-3">
            {report.requirement_assessments.map((item, index) => (
              <div key={`${index}-${item.requirement}`} className="rounded-lg bg-panel p-3 text-sm">
                <div className="flex flex-wrap items-center gap-2">
                  <p className="font-medium text-ink">{item.requirement}</p>
                  <span className="tag bg-panel-2 text-ink-2">{importanceLabel(item.importance, locale)}</span>
                  <span className="tag bg-panel-2 text-ink-2">{evidenceLabel(item.evidence, locale)}</span>
                </div>
                {item.limitation && <p className="mt-1.5 text-ink-2">{item.limitation}</p>}
                {item.jd_evidence.length > 0 && (
                  <div className="mt-2">
                    <p className="text-xs font-semibold text-ink-3">{l("JD 依据", "Job-description evidence")}</p>
                    <div className="mt-1"><BulletList items={item.jd_evidence} /></div>
                  </div>
                )}
                {item.resume_evidence.length > 0 && (
                  <div className="mt-2">
                    <p className="text-xs font-semibold text-ink-3">{l("简历依据", "Résumé evidence")}</p>
                    <div className="mt-1"><BulletList items={item.resume_evidence} /></div>
                  </div>
                )}
              </div>
            ))}
          </div>
        </details>
      )}

      <section aria-labelledby="section-reviews-title">
        <h3 id="section-reviews-title" className="section-label mb-2">{l("分区优化建议", "Section-by-section advice")}</h3>
        <div className="space-y-2">
          {report.section_reviews.map((section) => {
            const open = expanded.has(section.section_id);
            const contentId = `adaptation-section-${section.section_id}`;
            return (
              <article key={section.section_id} className="overflow-hidden rounded-xl border border-line bg-panel-2">
                <button
                  type="button"
                  onClick={() => toggle(section.section_id)}
                  aria-expanded={open}
                  aria-controls={contentId}
                  className="flex w-full items-start gap-3 px-3.5 py-3 text-left"
                >
                  <span className="min-w-0 flex-1">
                    <span className="block break-words text-sm font-semibold text-ink">{section.title}</span>
                    <span className="mt-0.5 block break-words text-xs text-ink-3">{section.conclusion}</span>
                  </span>
                  <span className="tag shrink-0 bg-panel text-ink-2">{assessmentLabel(section.assessment, locale)}</span>
                  <span aria-hidden="true" className={`mt-1 text-xs text-ink-3 transition-transform ${open ? "rotate-180" : ""}`}>⌄</span>
                </button>
                {open && (
                  <div id={contentId} className="space-y-3 border-t border-line-2 px-3.5 py-3">
                    {section.rationale && <p className="whitespace-pre-wrap break-words text-sm text-ink-2">{section.rationale}</p>}
                    {section.preparation_points.length > 0 && (
                      <div>
                        <p className="mb-1 text-xs font-semibold text-ink">{l("面试重点准备", "Interview preparation priorities")}</p>
                        <BulletList items={section.preparation_points} />
                      </div>
                    )}
                    {section.improvements.length > 0 && (
                      <div>
                        <p className="mb-1 text-xs font-semibold text-ink">{l("修改动作", "Editing actions")}</p>
                        <BulletList items={section.improvements} />
                      </div>
                    )}
                    {section.rewrites.length > 0 && (
                      <div className="space-y-2">
                        <p className="text-xs font-semibold text-ink">{l("原文对照改写", "Original-to-suggested rewrites")}</p>
                        {section.rewrites.map((rewrite, index) => (
                          <RewriteCard key={`${section.section_id}-rewrite-${index}`} rewrite={rewrite} />
                        ))}
                      </div>
                    )}
                  </div>
                )}
              </article>
            );
          })}
        </div>
      </section>
    </div>
  );
}

function GapReport({ response }: { response: ResumeAdaptationResponse }) {
  const l = useLocalizer();
  const { locale } = useLocale();
  const report = response.report;
  if (!report || report.mode !== "gap_brief") return null;
  return (
    <div className="space-y-5">
      <section aria-labelledby="major-gaps-title">
        <h3 id="major-gaps-title" className="section-label mb-2">{l("优先补齐的差距", "Priority gaps")}</h3>
        <div className="space-y-2">
          {report.major_gaps.map((gap, index) => (
            <div key={`${index}-${gap.requirement}`} className="rounded-xl border border-warn/25 bg-warn-soft p-3 text-sm">
              <div className="flex flex-wrap items-center gap-2">
                <p className="font-semibold text-ink">{gap.requirement}</p>
                <span className="tag bg-panel text-warn">{evidenceLabel(gap.evidence, locale)}</span>
              </div>
              <p className="mt-1.5 text-ink-2">{gap.basis}</p>
            </div>
          ))}
        </div>
      </section>
      <section aria-labelledby="gap-next-steps-title">
        <h3 id="gap-next-steps-title" className="section-label mb-2">{l("下一步", "Next steps")}</h3>
        <BulletList items={report.next_steps} />
      </section>
    </div>
  );
}

function ModalFrame({
  title,
  children,
  onCancel,
}: {
  title: string;
  children: ReactNode;
  onCancel: () => void;
}) {
  const l = useLocalizer();
  const dialogRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    const previouslyFocused = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    dialogRef.current?.focus();
    return () => previouslyFocused?.focus();
  }, []);

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape") {
      event.preventDefault();
      onCancel();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>(
      "button:not([disabled]), [href], input:not([disabled]), select:not([disabled])",
    ) ?? []);
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (document.activeElement === dialogRef.current) {
      event.preventDefault();
      (event.shiftKey ? last : first).focus();
    } else if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  return (
    <div className="fixed inset-0 z-[70] flex items-center justify-center p-4" role="presentation">
      <button type="button" tabIndex={-1} aria-label={l("关闭确认", "Close confirmation")} onClick={onCancel} className="absolute inset-0 cursor-default bg-black/45" />
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="adaptation-confirmation-title"
        tabIndex={-1}
        onKeyDown={handleKeyDown}
        className="relative z-10 w-full max-w-md rounded-2xl border border-line bg-panel p-5 outline-none"
        style={{ boxShadow: "var(--shadow-pop)" }}
      >
        <h2 id="adaptation-confirmation-title" className="text-base font-semibold">{title}</h2>
        {children}
      </div>
    </div>
  );
}

function ConfirmationDialog({
  kind,
  busy,
  disclosure,
  estimatedInputTokens,
  continuesWithoutResearch,
  onCancel,
  onConfirm,
}: {
  kind: Exclude<Confirmation, null>;
  busy: boolean;
  disclosure: ResumeAdaptationModelDisclosure | null;
  estimatedInputTokens: number | null;
  continuesWithoutResearch: boolean;
  onCancel: () => void;
  onConfirm: () => void;
}) {
  const l = useLocalizer();
  const { locale } = useLocale();
  const summarized = kind === "summarized";
  return (
    <ModalFrame
      title={summarized
        ? continuesWithoutResearch ? l("确认使用压缩摘要且不含调研", "Confirm summarized analysis without research") : l("确认使用压缩摘要", "Confirm summarized analysis")
        : l("确认不含公司调研", "Confirm analysis without company research")}
      onCancel={onCancel}
    >
      <p className="mt-2 text-sm leading-6 text-ink-2">
        {summarized
          ? l(`这会额外调用模型；超长文件可能分块多次。报告没有逐段点评与原文对照改写。${continuesWithoutResearch ? "本次报告也不包含公司调研背景。" : ""}报告会显著标注实际降级形态，确认只对本次生成有效。`, `This uses an additional model call and may split very long files into several chunks. The report omits line-by-line commentary and original-to-suggested rewrites.${continuesWithoutResearch ? " It also excludes company research." : ""} The report clearly marks the actual limitation; confirmation applies only to this generation.`)
          : l("本次报告不包含公司调研背景，只会使用完整 JD 与简历。报告会显著标为“未结合公司调研”，确认只对本次生成有效。", "This report excludes company research and uses only the complete job description and résumé. It is clearly marked as not using company research; confirmation applies only to this generation.")}
      </p>
      {disclosure && (
        <p className="mt-3 rounded-lg bg-panel-2 px-3 py-2 text-xs leading-5 text-ink-2">
          {summarized
            ? continuesWithoutResearch
              ? l(`本次流程会把简历可提取文本和完整 JD 发送给 ${disclosure.label}，不会发送公司调研结论；简历摘要会用于后续适配。`, `This flow sends extractable résumé text and the complete job description to ${disclosure.label}, without company-research findings. The résumé summary is used for adaptation.`)
              : l(`本次流程会把简历可提取文本、完整 JD 和可用调研结论发送给 ${disclosure.label}；简历摘要会用于后续适配。`, `This flow sends extractable résumé text, the complete job description, and available research findings to ${disclosure.label}. The résumé summary is used for adaptation.`)
            : l(`本次会把完整 JD 与简历可提取文本发送给 ${disclosure.label}，不会发送公司调研结论。`, `This sends the complete job description and extractable résumé text to ${disclosure.label}, without company-research findings.`)}
          {!summarized && estimatedInputTokens !== null
            ? l(` 预计适配输入约 ${formatNumber(estimatedInputTokens, "zh-CN")} tokens。`, ` Estimated adaptation input: about ${formatNumber(estimatedInputTokens, locale)} tokens.`)
            : ""}
        </p>
      )}
      <div className="mt-5 flex justify-end gap-2">
        <button type="button" onClick={onCancel} disabled={busy} className="btn">{l("取消", "Cancel")}</button>
        <button type="button" onClick={onConfirm} disabled={busy} className="btn-primary">
          {busy ? l("正在提交…", "Submitting…") : summarized ? l("确认并生成摘要报告", "Confirm and generate summary report") : l("确认并生成无调研报告", "Confirm and generate without research")}
        </button>
      </div>
    </ModalFrame>
  );
}

function InputPreviewDialog({ preview, onCancel }: { preview: ResumeAdaptationInputPreview; onCancel: () => void }) {
  const l = useLocalizer();
  return (
    <ModalFrame title={l(`发送内容预览 · ${preview.resume_name}`, `Model-input preview · ${preview.resume_name}`)} onCancel={onCancel}>
      <p className="mt-1 text-xs text-ink-3">
        {preview.input_form === "summarized" ? l("以下是将发送给模型的压缩摘要，不是简历原文。", "This is the compressed summary sent to the model, not the original résumé text.") : l("以下是从文件中提取并发送给模型的完整文字，不包含视觉排版。", "This is the full text extracted from the file and sent to the model; visual layout is not included.")}
      </p>
      <pre className="mt-3 max-h-[60vh] overflow-auto whitespace-pre-wrap break-words rounded-xl bg-panel-2 p-3 text-xs leading-5 text-ink-2">{preview.text}</pre>
      {preview.host_limitations.length > 0 && <div className="mt-3"><BulletList items={preview.host_limitations} /></div>}
      <div className="mt-4 flex justify-end">
        <button
          type="button"
          onClick={onCancel}
          aria-label={l("关闭发送内容预览", "Close model-input preview")}
          className="inline-flex h-8 w-8 items-center justify-center rounded-lg text-lg leading-none text-ink-3 transition-colors hover:bg-panel-2 hover:text-ink"
        >
          ×
        </button>
      </div>
    </ModalFrame>
  );
}

export function ResumeAdaptationPanel({
  applicationId,
  editRevision,
  onApplicationChanged,
  onEditJd,
  onResearchAction,
  onConfigureResearch,
  onConfigureModel,
  onStatusChange,
}: ResumeAdaptationPanelProps) {
  const l = useLocalizer();
  const { locale } = useLocale();
  const [response, setResponse] = useState<ResumeAdaptationResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState<BusyState>(null);
  const [error, setError] = useState("");
  const [selectedResumeId, setSelectedResumeId] = useState<number | null>(null);
  const [confirmation, setConfirmation] = useState<Confirmation>(null);
  const [preview, setPreview] = useState<ResumeAdaptationInputPreview | null>(null);
  const [elapsedSeconds, setElapsedSeconds] = useState(0);
  const [researchFollowupActive, setResearchFollowupActive] = useState(false);
  const [pendingNoResearchIntent, setPendingNoResearchIntent] = useState(false);
  const mountedRef = useRef(true);
  const applicationIdRef = useRef(applicationId);
  const readEpochRef = useRef(0);
  const readControllerRef = useRef<AbortController | null>(null);
  const generationControllerRef = useRef<AbortController | null>(null);
  const previewControllerRef = useRef<AbortController | null>(null);
  const continueAfterResearchRef = useRef(false);
  const versionChooserRef = useRef<HTMLDetailsElement | null>(null);

  const adoptResponse = useCallback((next: ResumeAdaptationResponse) => {
    setResponse(next);
    setSelectedResumeId((current) => {
      const available = next.resume_options.map((resume) => resume.id);
      if (current !== null && available.includes(current)) return current;
      return next.bound_resume?.id
        ?? next.recommended_resume_id
        ?? next.resume_options[0]?.id
        ?? null;
    });
  }, []);

  const readCurrent = useCallback(async (quiet = false): Promise<ResumeAdaptationResponse | null> => {
    const requestApplicationId = applicationId;
    const epoch = ++readEpochRef.current;
    readControllerRef.current?.abort();
    const controller = new AbortController();
    readControllerRef.current = controller;
    if (!quiet) setLoading(true);
    try {
      const next = await getResumeAdaptation(requestApplicationId, { signal: controller.signal });
      if (!mountedRef.current || applicationIdRef.current !== requestApplicationId
          || readEpochRef.current !== epoch) return null;
      adoptResponse(next);
      setError("");
      return next;
    } catch (caught) {
      if (!isAbortError(caught) && mountedRef.current
          && applicationIdRef.current === requestApplicationId && readEpochRef.current === epoch) {
        setError(caught instanceof Error ? caught.message : l("简历优化状态读取失败", "Could not read résumé-optimization status"));
      }
      return null;
    } finally {
      if (!quiet && mountedRef.current && applicationIdRef.current === requestApplicationId
          && readEpochRef.current === epoch) setLoading(false);
    }
  }, [adoptResponse, applicationId, l]);

  const runGeneration = useCallback(async ({
    refresh = false,
    acceptNoResearch = false,
    acceptSummarized = false,
    expectedResumeId,
  }: {
    refresh?: boolean;
    acceptNoResearch?: boolean;
    acceptSummarized?: boolean;
    expectedResumeId?: number | null;
  } = {}) => {
    const requestApplicationId = applicationId;
    const resumeId = expectedResumeId ?? response?.bound_resume?.id ?? selectedResumeId;
    if (resumeId === null) {
      setError(l("请先确认本次使用的简历版本", "Confirm which résumé version to use first"));
      return;
    }
    generationControllerRef.current?.abort();
    const controller = new AbortController();
    generationControllerRef.current = controller;
    let timedOut = false;
    const timeout = window.setTimeout(() => {
      timedOut = true;
      controller.abort();
    }, ADAPTATION_WAIT_TIMEOUT_MS);
    setBusy("generating");
    setElapsedSeconds(0);
    setError("");
    try {
      const next = await generateResumeAdaptation(requestApplicationId, {
        refresh,
        expected_resume_id: resumeId,
        accept_no_research: acceptNoResearch,
        accept_summarized: acceptSummarized,
      }, { signal: controller.signal });
      if (!mountedRef.current || applicationIdRef.current !== requestApplicationId) return;
      adoptResponse(next);
      setPendingNoResearchIntent(
        acceptNoResearch
        && next.state === "insufficient_model_capacity"
        && next.summarization_available,
      );
    } catch (caught) {
      if (!mountedRef.current || applicationIdRef.current !== requestApplicationId) return;
      if (isAbortError(caught)) {
        setError(timedOut
          ? l("已停止等待。后台会继续完成当前任务，稍后重新打开本页即可查看结果。", "Stopped waiting. The task continues in the background; reopen this page later to see the result.")
          : l("已停止等待。如果任务已经开始，后台仍会继续完成。", "Stopped waiting. If the task already started, it continues in the background."));
      } else if (caught instanceof HttpError && caught.status === 404) {
        setError(l("这个岗位已不存在，请刷新求职进展", "This role no longer exists. Refresh Application Tracker."));
      } else {
        setError(caught instanceof Error ? caught.message : l("简历优化建议生成失败", "Could not generate résumé recommendations"));
      }
    } finally {
      window.clearTimeout(timeout);
      if (generationControllerRef.current === controller) {
        generationControllerRef.current = null;
      }
      if (mountedRef.current && applicationIdRef.current === requestApplicationId) {
        setBusy(null);
        setConfirmation(null);
      }
    }
  }, [adoptResponse, applicationId, l, response?.bound_resume?.id, selectedResumeId]);

  useEffect(() => {
    mountedRef.current = true;
    applicationIdRef.current = applicationId;
    setResponse(null);
    setError("");
    setConfirmation(null);
    setPreview(null);
    setBusy(null);
    setElapsedSeconds(0);
    setResearchFollowupActive(false);
    setPendingNoResearchIntent(false);
    continueAfterResearchRef.current = false;
    generationControllerRef.current?.abort();
    previewControllerRef.current?.abort();
    void readCurrent();
    return () => {
      readControllerRef.current?.abort();
      generationControllerRef.current?.abort();
      previewControllerRef.current?.abort();
    };
  }, [applicationId, readCurrent]);

  useEffect(() => () => {
    mountedRef.current = false;
    continueAfterResearchRef.current = false;
  }, []);

  useEffect(() => {
    if (busy !== "generating") return;
    const startedAt = Date.now();
    const update = () => setElapsedSeconds(Math.floor((Date.now() - startedAt) / 1_000));
    update();
    const timer = window.setInterval(update, 1_000);
    return () => window.clearInterval(timer);
  }, [busy]);

  const generationRunning = busy === "generating"
    || response?.state === "generation_running"
    || researchFollowupActive;
  const panelStatus: ResumeAdaptationPanelStatus = generationRunning
    ? "running"
    : response?.state === "ok" ? "ready" : "idle";
  useEffect(() => {
    onStatusChange?.(panelStatus);
  }, [onStatusChange, panelStatus]);

  useEffect(() => {
    if (!researchFollowupActive || busy === "research" || busy === "generating") return;
    return startResumeAdaptationResearchPolling({
      intervalMs: RESEARCH_POLL_INTERVAL_MS,
      read: () => readCurrent(true),
      onReady: async (next) => {
        const shouldContinue = continueAfterResearchRef.current;
        continueAfterResearchRef.current = false;
        setResearchFollowupActive(false);
        if (shouldContinue) {
          await runGeneration({ expectedResumeId: next.bound_resume?.id });
        }
      },
      onTerminal: () => {
        continueAfterResearchRef.current = false;
        setResearchFollowupActive(false);
      },
    });
  }, [busy, readCurrent, researchFollowupActive, runGeneration]);

  useEffect(() => {
    if (response?.state !== "research_running" || researchFollowupActive
        || busy === "research" || busy === "generating") return;
    return startResumeAdaptationResearchPolling({
      intervalMs: RESEARCH_POLL_INTERVAL_MS,
      read: () => readCurrent(true),
      onReady: () => undefined,
      onTerminal: () => undefined,
    });
  }, [busy, readCurrent, researchFollowupActive, response?.state]);

  useEffect(() => {
    if (response?.state !== "generation_running" || busy === "generating") return;
    return startResumeAdaptationResearchPolling({
      intervalMs: RESEARCH_POLL_INTERVAL_MS,
      read: () => readCurrent(true),
      onReady: () => undefined,
      onTerminal: () => undefined,
    });
  }, [busy, readCurrent, response?.state]);

  async function bindSelectedResume() {
    if (selectedResumeId === null || busy !== null) return;
    const requestApplicationId = applicationId;
    setBusy("binding");
    setError("");
    try {
      const binding = await bindApplicationResume(requestApplicationId, {
        resume_id: selectedResumeId,
        expected_edit_revision: editRevision,
      });
      if (!mountedRef.current || applicationIdRef.current !== requestApplicationId) return;
      await onApplicationChanged?.(binding);
      if (!mountedRef.current || applicationIdRef.current !== requestApplicationId) return;
      const next = await readCurrent(true);
      if (!mountedRef.current || applicationIdRef.current !== requestApplicationId) return;
      if (!next) setError(l("简历版本已绑定，但当前简历优化状态读取失败，请重试", "Résumé version was selected, but its optimization status could not be read. Try again."));
    } catch (caught) {
      if (!mountedRef.current || applicationIdRef.current !== requestApplicationId) return;
      if (caught instanceof HttpError && caught.status === 409) {
        setError(l("岗位或简历已在另一处更新，已重新读取当前选择。请核对后再继续。", "The role or résumé changed elsewhere. The current selection was reloaded; verify it before continuing."));
        await readCurrent(true);
      } else {
        setError(caught instanceof Error ? caught.message : l("简历版本绑定失败", "Could not select this résumé version"));
      }
    } finally {
      if (mountedRef.current && applicationIdRef.current === requestApplicationId) setBusy(null);
    }
  }

  async function triggerResearch(continueAfter: boolean) {
    const action = response?.research?.action;
    if (!action || !onResearchAction || busy !== null) {
      if (!onResearchAction) setError(l("当前页面尚未接入公司调研动作", "Company-research actions are not available on this page"));
      return;
    }
    continueAfterResearchRef.current = continueAfter;
    setResearchFollowupActive(true);
    setBusy("research");
    setError("");
    try {
      await onResearchAction(action);
      await readCurrent(true);
    } catch (caught) {
      continueAfterResearchRef.current = false;
      setResearchFollowupActive(false);
      setError(caught instanceof Error ? caught.message : l("公司调研启动失败", "Could not start company research"));
    } finally {
      if (mountedRef.current) setBusy(null);
    }
  }

  async function openPreview() {
    if (busy !== null) return;
    const requestApplicationId = applicationId;
    previewControllerRef.current?.abort();
    const controller = new AbortController();
    previewControllerRef.current = controller;
    setBusy("preview");
    setError("");
    try {
      const loaded = await getResumeAdaptationInputPreview(requestApplicationId, {
        signal: controller.signal,
      });
      if (mountedRef.current && applicationIdRef.current === requestApplicationId) setPreview(loaded);
    } catch (caught) {
      if (mountedRef.current && !isAbortError(caught)) {
        setError(caught instanceof Error ? caught.message : l("发送内容预览加载失败", "Could not load the model-input preview"));
      }
    } finally {
      if (previewControllerRef.current === controller) {
        previewControllerRef.current = null;
      }
      if (mountedRef.current && applicationIdRef.current === requestApplicationId) setBusy(null);
    }
  }

  function openVersionChooser() {
    versionChooserRef.current?.setAttribute("open", "");
    window.requestAnimationFrame(() => {
      versionChooserRef.current?.querySelector<HTMLSelectElement>("select")?.focus();
    });
  }

  if (loading && !response) {
    return <p role="status" className="p-6 text-center text-sm text-ink-3">{l("正在读取简历优化状态…", "Loading résumé-optimization status…")}</p>;
  }
  if (!response) {
    return (
      <div className="p-6 text-center text-sm">
        <p role="alert" className="text-bad">{error || l("暂时无法读取简历优化状态", "Résumé-optimization status is temporarily unavailable")}</p>
        <button type="button" onClick={() => void readCurrent()} className="btn mt-3">{l("重试", "Retry")}</button>
      </div>
    );
  }

  const view = resumeAdaptationStateView(response, locale);
  const selectedResume = response.resume_options.find((resume) => resume.id === selectedResumeId) ?? null;
  const hasDifferentSelection = selectedResumeId !== null && selectedResumeId !== response.bound_resume?.id;
  const settingsLink = "#/settings";

  return (
    <div className="space-y-4">
      {error && <div role="alert" className="rounded-xl bg-bad-soft px-3 py-2 text-sm text-bad">{error}</div>}
      {busy === "generating" && (
        <div role="status" aria-live="polite" className="flex flex-wrap items-center justify-between gap-3 rounded-xl bg-info-soft px-3 py-2 text-sm text-info">
          <div>
            <p className="font-medium">{l("正在生成简历优化建议 · 已等待", "Generating résumé recommendations · elapsed")} {formatAdaptationElapsed(elapsedSeconds, locale)}</p>
            <p className="mt-0.5 text-xs">{l("通常需要约 1 分钟，材料长度和所选模型会影响耗时，最长等待 3 分钟。停止等待或离开页面后，后台仍会继续完成当前任务。", "This usually takes about a minute; material length and model choice affect duration, with a three-minute wait limit. The task continues in the background if you stop waiting or leave.")}</p>
          </div>
          <button
            type="button"
            onClick={() => generationControllerRef.current?.abort()}
            className="btn btn-sm shrink-0"
          >
            {l("停止等待", "Stop waiting")}
          </button>
        </div>
      )}

      {response.envelope?.research_mode === "no_research" && (
        <div role="status" className="rounded-xl border border-warn/25 bg-warn-soft px-3 py-2 text-sm font-medium text-warn">{l("未结合公司调研", "Company research not included")}</div>
      )}
      {response.envelope?.resume_input_form === "summarized" && (
        <div role="status" className="rounded-xl border border-warn/25 bg-warn-soft px-3 py-2 text-sm font-medium text-warn">{l("基于压缩摘要，非全文逐段分析", "Based on a compressed summary, not full-text line-by-line analysis")}</div>
      )}

      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="flex flex-wrap items-center gap-2">
            <p className="break-words text-sm text-ink">
              <span className="section-label">{l("当前材料：", "Current material: ")}</span>
              <span className="font-medium">{response.bound_resume?.name ?? selectedResume?.name ?? l("尚未确认简历版本", "Résumé version not confirmed")}</span>
            </p>
            {response.model_input_preview_available && (
              <button type="button" onClick={() => void openPreview()} disabled={busy !== null} className="btn btn-sm">
                {busy === "preview" ? l("正在读取…", "Loading…") : l("查看将发送给模型的文本", "View model-input text")}
              </button>
            )}
          </div>
          {response.state === "ok" && response.report && response.envelope && (
            <p className="mt-1 text-xs text-ink-3">
              {response.research && <>{l("调研：", "Research: ")}{researchStatusLabel(response.research, locale)}</>}
              {response.research?.fresh_until ? l(` · 有效至 ${formatAdaptationDateTime(response.research.fresh_until, locale)}`, ` · valid until ${formatAdaptationDateTime(response.research.fresh_until, locale)}`) : ""}
              {l(` · 生成时间：${formatAdaptationDateTime(response.envelope.generated_time, locale)}`, ` · generated ${formatAdaptationDateTime(response.envelope.generated_time, locale)}`)}
            </p>
          )}
        </div>
        {response.state === "ok" && response.report && response.envelope && (
          <button type="button" onClick={() => void runGeneration({ refresh: true })} disabled={busy !== null} className="btn-primary btn-sm shrink-0">
            {busy === "generating" ? l("正在生成…", "Generating…") : l("重新生成", "Regenerate")}
          </button>
        )}
      </div>

      {response.state === "ok" && response.report && response.envelope ? (
        <div className="space-y-5">
          <Summary sentences={response.report.summary_sentences} />
          {response.report.mode === "full"
            ? <FullReport key={`${response.envelope.generated_time}-${response.envelope.resume_id}`} response={response} />
            : <GapReport response={response} />}
          {response.report.mode === "gap_brief" && (
            <div className="flex flex-wrap gap-2">
              {response.resume_options.length > 1 && (
                <button type="button" onClick={openVersionChooser} className="btn">{l("更换简历版本", "Change résumé version")}</button>
              )}
              <a href={libraryHref(applicationId)} className="btn">{l("上传岗位专属版", "Upload role-specific version")}</a>
            </div>
          )}
          <TrustLimitations response={response} />
        </div>
      ) : (
        <section className={`rounded-xl border p-4 ${toneClass(view.tone)}`}>
          <h3 className="font-semibold text-ink">{view.title}</h3>
          <p className="mt-1 text-sm text-ink-2">{view.description}</p>
          {response.message && <p className="mt-2 text-xs opacity-80">{response.message}</p>}

          {response.state === "resume_selection_required" && (
            <fieldset disabled={busy !== null} className="mt-4 space-y-2">
              <legend className="sr-only">{l("选择简历版本", "Choose résumé version")}</legend>
              {response.resume_options.map((resume) => (
                <label key={resume.id} className="flex cursor-pointer items-start gap-2 rounded-lg bg-panel px-3 py-2 text-sm text-ink-2">
                  <input
                    type="radio"
                    name={`resume-adaptation-${applicationId}`}
                    value={resume.id}
                    checked={selectedResumeId === resume.id}
                    onChange={() => setSelectedResumeId(resume.id)}
                    className="mt-0.5"
                  />
                  <span>
                    <span className="font-medium text-ink">{resume.name}</span>
                    {resume.id === response.recommended_resume_id && <span className="ml-2 text-xs text-info">{l("推荐", "Recommended")}</span>}
                  </span>
                </label>
              ))}
            </fieldset>
          )}

          {response.model_disclosure && (response.state === "ready" || response.state === "insufficient_model_capacity") && (
            <p className="mt-3 rounded-lg bg-panel px-3 py-2 text-xs text-ink-2">
              {response.state === "ready" ? l("点击生成后会调用模型。", "The model is called after you choose Generate. ") : l("检查模型容量不会调用模型。", "Checking model capacity does not call the model. ")}
              {l("简历可提取全文、完整 JD 和可用调研结论将发送给", "Extractable résumé text, the complete job description, and available research findings are sent to")} {response.model_disclosure.label}。
              {response.estimated_input_tokens !== null ? l(` 预计输入约 ${formatNumber(response.estimated_input_tokens, "zh-CN")} tokens。`, ` Estimated input: about ${formatNumber(response.estimated_input_tokens, locale)} tokens.`) : ""}
            </p>
          )}

          {(response.state === "research_required" || response.state === "research_failed") && response.model_disclosure && (
            <p className="mt-3 rounded-lg bg-panel px-3 py-2 text-xs text-ink-2">
              {l("“生成调研并继续”会先完成调研；调研成功且本页仍打开时，会把可提取的简历全文、完整 JD 和新调研结论发送给", "Generate research and continue completes research first. If research succeeds while this page remains open, extractable résumé text, the complete job description, and new research findings are sent to")} {response.model_disclosure.label}{l("，然后自动生成优化建议。输入量会在调研内容准备好后计算。", ", then recommendations are generated automatically. Input size is calculated after research is ready.")}
            </p>
          )}

          <div className="mt-4 flex flex-wrap gap-2">
            {response.state === "ready" && (
              <button type="button" onClick={() => void runGeneration()} disabled={busy !== null} className="btn-primary">{l("生成优化建议", "Generate recommendations")}</button>
            )}
            {response.state === "resume_selection_required" && (
              <button type="button" onClick={() => void bindSelectedResume()} disabled={busy !== null || selectedResumeId === null} className="btn-primary">
                {busy === "binding" ? l("正在绑定…", "Selecting…") : l("确认使用此版本", "Use this version")}
              </button>
            )}
            {response.state === "no_resume" && <a href={libraryHref(applicationId)} className="btn-primary">{l("去上传简历", "Upload résumé")}</a>}
            {response.state === "resume_reupload_required" && <a href={libraryHref(applicationId, response.bound_resume?.id)} className="btn-primary">{l("重新上传这份简历", "Reupload this résumé")}</a>}
            {response.state === "missing_jd" && <button type="button" onClick={onEditJd} className="btn-primary">{l("补充岗位 JD", "Add job description")}</button>}
            {(response.state === "research_required" || response.state === "research_failed") && (
              response.model_disclosure
                ? <>
                    <button type="button" onClick={() => void triggerResearch(true)} disabled={busy !== null || !response.research?.action || !onResearchAction} className="btn-primary">
                      {busy === "research" ? l("正在启动调研…", "Starting research…") : l("生成调研并继续", "Generate research and continue")}
                    </button>
                    <button type="button" onClick={() => void triggerResearch(false)} disabled={busy !== null || !response.research?.action || !onResearchAction} className="btn">{l("仅生成调研", "Generate research only")}</button>
                  </>
                : (onConfigureModel
                    ? <button type="button" onClick={onConfigureModel} className="btn-primary">{l("配置模型后生成调研", "Configure model to generate research")}</button>
                    : <a href={settingsLink} className="btn-primary">{l("配置模型后生成调研", "Configure model to generate research")}</a>)
            )}
            {response.state === "research_running" && <span role="status" className="text-sm">{l("正在刷新调研进度…", "Refreshing research progress…")}</span>}
            {response.state === "generation_running" && <span role="status" className="text-sm">{l("正在读取后台生成进度…", "Loading background generation progress…")}</span>}
            {(response.state === "research_disabled" || response.state === "research_unavailable") && response.no_research_fallback_available && (
              <button type="button" onClick={() => setConfirmation("no_research")} disabled={busy !== null} className="btn-primary">{l("仅按 JD 与简历继续", "Continue with job description and résumé only")}</button>
            )}
            {(response.state === "research_disabled" || response.state === "research_unavailable") && (
              onConfigureResearch
                ? <button type="button" onClick={onConfigureResearch} className="btn">{l("检查调研设置", "Check research settings")}</button>
                : <a href={settingsLink} className="btn">{l("检查调研设置", "Check research settings")}</a>
            )}
            {response.state === "model_required" && (
              onConfigureModel
                ? <button type="button" onClick={onConfigureModel} className="btn-primary">{l("配置模型", "Configure model")}</button>
                : <a href={settingsLink} className="btn-primary">{l("配置模型", "Configure model")}</a>
            )}
            {response.state === "insufficient_model_capacity" && response.summarization_available && (
              <button type="button" onClick={() => setConfirmation("summarized")} disabled={busy !== null} className="btn-primary">{l("使用压缩摘要", "Use compressed summary")}</button>
            )}
            {response.state === "insufficient_model_capacity" && (
              onConfigureModel
                ? <button type="button" onClick={onConfigureModel} className="btn">{l("更换模型", "Change model")}</button>
                : <a href={settingsLink} className="btn">{l("更换模型", "Change model")}</a>
            )}
            {(response.state === "invalid_model_output" || response.state === "provider_error") && (
              <button type="button" onClick={() => void runGeneration()} disabled={busy !== null} className="btn-primary">{l("重新尝试", "Try again")}</button>
            )}
            {response.state === "stale" && <button type="button" onClick={() => void readCurrent()} disabled={busy !== null} className="btn-primary">{l("读取当前材料", "Load current materials")}</button>}
          </div>
        </section>
      )}

      {response.state === "ok" && response.resume_options.length > 1 && (
        <details ref={versionChooserRef} className="rounded-xl border border-line bg-panel-2 px-3 py-2 text-sm">
          <summary className="cursor-pointer font-medium text-ink-2">{l("更换简历版本", "Change résumé version")}</summary>
          <div className="mt-3 flex flex-col gap-2 sm:flex-row">
            <select
              aria-label={l("选择新的简历版本", "Choose a new résumé version")}
              value={selectedResumeId ?? ""}
              onChange={(event) => setSelectedResumeId(event.target.value ? Number(event.target.value) : null)}
              className="input min-w-0 flex-1"
            >
              {response.resume_options.map((resume) => <option key={resume.id} value={resume.id}>{resume.name}</option>)}
            </select>
            <button type="button" onClick={() => void bindSelectedResume()} disabled={!hasDifferentSelection || busy !== null} className="btn-primary">
              {busy === "binding" ? l("正在切换…", "Switching…") : l("确认切换版本", "Confirm version change")}
            </button>
          </div>
        </details>
      )}

      {confirmation && (
        <ConfirmationDialog
          kind={confirmation}
          busy={busy === "generating"}
          disclosure={response.model_disclosure}
          estimatedInputTokens={response.estimated_input_tokens}
          continuesWithoutResearch={confirmation === "summarized" && pendingNoResearchIntent}
          onCancel={() => { if (busy !== "generating") setConfirmation(null); }}
          onConfirm={() => void runGeneration({
            acceptNoResearch: confirmation === "no_research"
              || (confirmation === "summarized" && pendingNoResearchIntent),
            acceptSummarized: confirmation === "summarized",
          })}
        />
      )}
      {preview && <InputPreviewDialog preview={preview} onCancel={() => setPreview(null)} />}
    </div>
  );
}
