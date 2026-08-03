import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import { del, getJson, postForm, putForm, putJson } from "../../shared/api/transport";
import { useLocalizer } from "../../i18n/useLocalizer";
import { useLocale } from "../../i18n/localePreference";
import { formatNumber } from "../../i18n/formatters";
import type { UiLocale } from "../../i18n/i18n";
import type {
  ResumeItem,
  ResumeJob,
  ResumeJobDismissResponse,
  ResumeText,
} from "../resumes/resumeContract";
import { resumeJobMessage, resumeMutationErrorMessage } from "../resumes/resumePresentation";
import type { Board, BoardItem } from "../timeline/timelineContract";
import { parseLibraryDeepLink } from "./libraryDeepLink";

function RefreshIcon({ className = "h-3.5 w-3.5" }: { className?: string }) {
  return (
    <svg viewBox="0 0 16 16" className={className} fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" strokeLinejoin="round">
      <path d="M13.4 8a5.4 5.4 0 1 1-1.5-3.8M13.5 2.8v2.6h-2.6" />
    </svg>
  );
}
function LoadingSpinner() {
  return (
    <span
      aria-hidden="true"
      className="inline-block h-4 w-4 shrink-0 animate-spin rounded-full border-2 border-current border-r-transparent"
    />
  );
}

function ResumeJobStatus({
  job,
  locale,
  dismissing,
  onDismiss,
}: {
  job: ResumeJob;
  locale: UiLocale;
  dismissing: boolean;
  onDismiss: (job: ResumeJob) => void;
}) {
  const l = useLocalizer();
  const stage = {
    queued: l("等待开始", "Waiting to start"),
    extracting: l("正在读取文档", "Reading document"),
    parsing: l("正在调用模型解析", "Parsing with model"),
    saving: l("正在保存结果", "Saving result"),
    completed: l("处理完成", "Completed"),
    failed: l("处理失败", "Failed"),
  }[job.stage];
  return (
    <div
      className={`rounded-xl border px-4 py-3 text-sm ${
        job.state === "failed"
          ? "border-bad/30 bg-bad-soft text-bad"
          : job.state === "completed"
            ? "border-ok/30 bg-ok-soft text-ok"
            : "border-warn/30 bg-warn-soft text-warn"
      }`}
    >
      <div className="flex items-start gap-2 font-medium">
        <div className="flex min-w-0 flex-1 items-center gap-2">
          {job.state === "processing" && <LoadingSpinner />}
          <span className="break-words">{job.name} · {stage}</span>
        </div>
        {job.state !== "processing" && (
          <button
            type="button"
            className="-mr-1 -mt-1 rounded-md px-2 py-1 text-base leading-none opacity-70 hover:bg-panel hover:opacity-100 disabled:cursor-wait"
            aria-label={`${l("关闭简历任务提示", "Dismiss résumé task")}: ${job.name}`}
            title={l("关闭这条任务提示", "Dismiss this task")}
            disabled={dismissing}
            onClick={() => onDismiss(job)}
          >
            ×
          </button>
        )}
      </div>
      <p className="mt-1 text-xs opacity-80">
        {resumeJobMessage(job, locale)}
      </p>
    </div>
  );
}

export function LibraryPage() {
  const l = useLocalizer();
  const { locale } = useLocale();
  const location = useLocation();
  const deepLink = useMemo(() => parseLibraryDeepLink(location.search), [location.search]);
  const [resumes, setResumes] = useState<ResumeItem[]>([]);
  const [resumeJobs, setResumeJobs] = useState<ResumeJob[]>([]);
  const [dismissingResumeJobs, setDismissingResumeJobs] = useState<Record<string, boolean>>({});
  const activeOrFailedResumeJobs = resumeJobs.filter((job) => job.state !== "completed");
  const completedResumeJobs = resumeJobs.filter((job) => job.state === "completed");
  const [error, setError] = useState("");
  const [loadError, setLoadError] = useState("");
  const [notice, setNotice] = useState("");
  const [loading, setLoading] = useState(true);
  const [resumesLoaded, setResumesLoaded] = useState(false);
  const [busy, setBusy] = useState<"resume" | null>(null);
  const errorRef = useRef<HTMLParagraphElement | null>(null);
  const refreshRunningRef = useRef(false);
  const refreshQueuedRef = useRef(false);
  const dismissedResumeJobIdsRef = useRef<Set<string>>(new Set());
  const resumeSectionRef = useRef<HTMLElement | null>(null);
  const resumeUploadCardRef = useRef<HTMLDivElement | null>(null);
  const focusedResumeDeepLinkRef = useRef("");

  // Upload form for general or application-bound résumé variants.
  const [resumeName, setResumeName] = useState("");
  const [resumeFile, setResumeFile] = useState<File | null>(null);
  const resumeFileRef = useRef<HTMLInputElement | null>(null);
  const [bindingType, setBindingType] = useState<"general" | "application">("general");
  const [resumeApplicationId, setResumeApplicationId] = useState<number | null>(null);
  const [positions, setPositions] = useState<BoardItem[]>([]);

  // Full text loads only on explicit request, keeping list responses lightweight.
  const [viewingResume, setViewingResume] = useState<ResumeText | null>(null);
  const [editedResumeText, setEditedResumeText] = useState("");
  const [savingResumeText, setSavingResumeText] = useState(false);
  const [resumeTextError, setResumeTextError] = useState("");
  const [viewingResumeId, setViewingResumeId] = useState<number | null>(null);
  const resumeTextAbortRef = useRef<AbortController | null>(null);
  const resumeTextCloseRef = useRef<HTMLButtonElement | null>(null);

  // Updating replaces content in place while retaining the variant name and binding.
  const [updatingId, setUpdatingId] = useState<number | null>(null);
  const resumeUpdateRef = useRef<HTMLInputElement | null>(null);
  const resumeUpdateTargetRef = useRef<number | null>(null);

  useEffect(() => {
    const input = resumeUpdateRef.current;
    if (!input) return;
    const cancel = () => setUpdatingId(null);
    input.addEventListener("cancel", cancel);
    return () => input.removeEventListener("cancel", cancel);
  }, []);

  useEffect(() => () => resumeTextAbortRef.current?.abort(), []);

  useEffect(() => {
    if (!viewingResume) return;
    resumeTextCloseRef.current?.focus();
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !savingResumeText) setViewingResume(null);
    };
    window.addEventListener("keydown", closeOnEscape);
    return () => window.removeEventListener("keydown", closeOnEscape);
  }, [savingResumeText, viewingResume]);

  useEffect(() => {
    const focusKey = `${location.key}:${location.search}`;
    if (focusedResumeDeepLinkRef.current === focusKey) return;
    const frame = window.requestAnimationFrame(() => {
      const requestedCard = deepLink.resumeId === null
        ? null
        : resumeSectionRef.current?.querySelector<HTMLElement>(
          `[data-resume-id="${deepLink.resumeId}"]`,
        ) ?? null;
      if (deepLink.resumeId !== null && requestedCard === null && !resumesLoaded) return;
      const target = requestedCard ?? resumeUploadCardRef.current;
      if (!target) return;
      target.scrollIntoView({ behavior: "smooth", block: "start" });
      target.focus({ preventScroll: true });
      focusedResumeDeepLinkRef.current = focusKey;
    });
    return () => window.cancelAnimationFrame(frame);
  }, [deepLink.resumeId, location.key, location.search, resumes.length, resumesLoaded]);

  const refresh = useCallback(async () => {
    if (refreshRunningRef.current) {
      refreshQueuedRef.current = true;
      return;
    }
    refreshRunningRef.current = true;
    try {
      do {
        refreshQueuedRef.current = false;
        const [resumeResult, resumeJobResult, boardResult] = await Promise.allSettled([
          getJson<{ items: ResumeItem[] }>("/api/resumes"),
          getJson<{ items: ResumeJob[] }>("/api/resumes/jobs", { cache: "no-store" }),
          getJson<Board>("/api/timeline/board"),
        ]);
        const failures: string[] = [];
        if (resumeResult.status === "fulfilled") {
          setResumes(resumeResult.value.items);
          setResumesLoaded(true);
        } else {
          failures.push(`${l("简历加载失败", "Failed to load résumés")}: ${resumeResult.reason instanceof Error ? resumeResult.reason.message : l("请稍后重试", "Try again later")}`);
        }
        if (resumeJobResult.status === "fulfilled") {
          const serverItems = resumeJobResult.value.items;
          const dismissedIds = dismissedResumeJobIdsRef.current;
          setResumeJobs(serverItems.filter((job) => !dismissedIds.has(job.job_id)));
        } else {
          failures.push(`${l("简历任务状态加载失败", "Failed to load résumé task status")}: ${resumeJobResult.reason instanceof Error ? resumeJobResult.reason.message : l("请稍后重试", "Try again later")}`);
        }
        if (boardResult.status === "fulfilled") {
          setPositions(Object.values(boardResult.value.columns).flat());
        } else {
          failures.push(`${l("岗位加载失败", "Failed to load roles")}: ${boardResult.reason instanceof Error ? boardResult.reason.message : l("请稍后重试", "Try again later")}`);
        }
        setLoadError(failures.join(l("；", "; ")));
        setLoading(false);
      } while (refreshQueuedRef.current);
    } finally {
      refreshRunningRef.current = false;
    }
  }, [l]);

  const resumeJobsRef = useRef<ResumeJob[]>([]);
  resumeJobsRef.current = resumeJobs;

  useEffect(() => {
    void refresh();
    // Server-owned tasks continue independently; this page only polls their state.
    const timer = setInterval(() => {
      if (resumeJobsRef.current.some((job) => job.state === "processing")) void refresh();
    }, 2000);
    const refreshOnReturn = () => {
      if (document.visibilityState === "visible") void refresh();
    };
    document.addEventListener("visibilitychange", refreshOnReturn);
    window.addEventListener("focus", refreshOnReturn);
    return () => {
      clearInterval(timer);
      document.removeEventListener("visibilitychange", refreshOnReturn);
      window.removeEventListener("focus", refreshOnReturn);
    };
  }, [refresh]);

  // Keep upload failures visible even when the user is lower on the page.
  useEffect(() => {
    if (error) errorRef.current?.scrollIntoView({ behavior: "smooth", block: "center" });
  }, [error]);

  async function removeResume(resume: ResumeItem) {
    if (!window.confirm(l(`删除简历「${resume.name}」？（归档处理，历史产物不受影响）`, `Delete “${resume.name}”? It will be archived; existing artifacts will not be affected.`))) return;
    setNotice("");
    try {
      const result = await del<{ cleanup_warning?: string }>(`/api/resumes/${resume.id}`);
      await refresh();
      if (result.cleanup_warning) setNotice(result.cleanup_warning);
    } catch (e) {
      setError(e instanceof Error ? e.message : l("删除失败", "Delete failed"));
    }
  }

  async function dismissResumeJob(job: ResumeJob) {
    if (job.state === "processing" || dismissingResumeJobs[job.job_id]) return;
    setDismissingResumeJobs((current) => ({ ...current, [job.job_id]: true }));
    try {
      await del<ResumeJobDismissResponse>(`/api/resumes/jobs/${job.job_id}`);
      dismissedResumeJobIdsRef.current.add(job.job_id);
      setResumeJobs((current) => current.filter((item) => item.job_id !== job.job_id));
      await refresh();
    } catch (e) {
      setError(e instanceof Error ? e.message : l("任务提示关闭失败", "Could not dismiss the task"));
    } finally {
      setDismissingResumeJobs((current) => {
        const next = { ...current };
        delete next[job.job_id];
        return next;
      });
    }
  }

  async function uploadResume() {
    if (!resumeFile || busy !== null) return;
    if (bindingType === "application" && resumeApplicationId === null) {
      setError(l("岗位专属简历需要选一个岗位", "Select a role for a role-specific résumé"));
      return;
    }
    setBusy("resume");
    setError("");
    setNotice("");
    try {
      const form = new FormData();
      form.append("file", resumeFile);
      if (resumeName.trim()) form.append("name", resumeName);
      if (bindingType === "application") {
        form.append("binding", "application");
        form.append("application_id", String(resumeApplicationId));
      }
      const r = await postForm<{ status: string; message?: string; job_id?: string }>("/api/resumes/upload", form);
      if (r.status !== "ok" && r.status !== "processing") {
        setError(resumeMutationErrorMessage(r.message, locale, "upload"));
      } else if (r.status === "processing") {
        setResumeName("");
        setResumeFile(null);
        if (resumeFileRef.current) resumeFileRef.current.value = "";
        setNotice(l("简历已进入后台处理，可以离开本页；返回后仍会显示当前阶段和结果。", "Your résumé is now processing in the background. You can leave this page and return to its current stage and result."));
        await refresh();
      } else if (r.status === "ok") {
        setNotice(l("简历已解析并保存，可以在下方查看提取到的文字。", "Your résumé has been parsed and saved. You can review the extracted text below."));
        await refresh();
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : l("上传失败", "Upload failed"));
    } finally {
      setBusy(null);
    }
  }

  function pickResumeUpdate(id: number) {
    if (updatingId !== null) return;
    resumeUpdateTargetRef.current = id;
    if (resumeUpdateRef.current) {
      resumeUpdateRef.current.value = "";
      resumeUpdateRef.current.click();
    }
  }

  async function viewResumeText(resume: ResumeItem) {
    resumeTextAbortRef.current?.abort();
    const controller = new AbortController();
    resumeTextAbortRef.current = controller;
    setViewingResumeId(resume.id);
    setError("");
    try {
      const result = await getJson<ResumeText>(`/api/resumes/${resume.id}/text`, {
        cache: "no-store",
        signal: controller.signal,
      });
      if (result.id === resume.id) {
        setViewingResume(result);
        setEditedResumeText(result.content_text);
        setResumeTextError("");
      }
    } catch (e) {
      if (!controller.signal.aborted) setError(e instanceof Error ? e.message : l("简历文字读取失败", "Could not load résumé text"));
    } finally {
      if (resumeTextAbortRef.current === controller) {
        resumeTextAbortRef.current = null;
        setViewingResumeId(null);
      }
    }
  }

  async function saveResumeText() {
    if (!viewingResume || savingResumeText) return;
    if (!editedResumeText.trim()) {
      setResumeTextError(l("校正版文字不能为空", "Corrected text cannot be empty"));
      return;
    }
    setSavingResumeText(true);
    setResumeTextError("");
    try {
      const updated = await putJson<ResumeText>(`/api/resumes/${viewingResume.id}/text`, {
        content_text: editedResumeText,
        expected_content_hash: viewingResume.content_hash,
      });
      setViewingResume(updated);
      setEditedResumeText(updated.content_text);
      setNotice(l("校正版文字已保存。", "Corrected text saved."));
      await refresh();
    } catch (e) {
      setResumeTextError(e instanceof Error ? e.message : l("校正版文字保存失败", "Could not save corrected text"));
    } finally {
      setSavingResumeText(false);
    }
  }

  async function doResumeUpdate(file: File | null) {
    const id = resumeUpdateTargetRef.current;
    resumeUpdateTargetRef.current = null;
    if (!file || id === null) return;
    setUpdatingId(id);
    setError("");
    try {
      const form = new FormData();
      form.append("file", file);
      const r = await putForm<{ status: string; message?: string; job_id?: string }>(`/api/resumes/${id}`, form);
      if (r.status !== "ok" && r.status !== "processing") {
        setError(resumeMutationErrorMessage(r.message, locale, "update"));
        if (r.status === "stale") await refresh();
      } else if (r.status === "processing") {
        setNotice(l("简历更新已进入后台处理，可以离开本页；旧版本会保留到新版本成功发布。", "The update is processing in the background. You can leave this page; the previous version remains available until the new one is published."));
        await refresh();
      } else {
        await refresh();
      }
    } catch (e) {
      setError(e instanceof Error ? e.message : l("更新失败", "Update failed"));
    } finally {
      setUpdatingId(null);
      if (resumeUpdateRef.current) resumeUpdateRef.current.value = "";
    }
  }

  return (
    <div className="flex flex-col gap-8">
      {error && (
        <div role="alert" className="flex items-start gap-3 rounded-xl bg-bad-soft px-4 py-3 text-sm text-bad">
          <p ref={errorRef} className="min-w-0 flex-1 scroll-mt-20 break-words">{error}</p>
          <button
            type="button"
            aria-label={l("关闭错误提示", "Dismiss error")}
            className="rounded-md px-2 py-1 text-base leading-none opacity-70 hover:bg-panel hover:opacity-100"
            onClick={() => setError("")}
          >
            ×
          </button>
        </div>
      )}
      {loadError && (
        <div role="alert" className="flex items-start gap-3 rounded-xl bg-bad-soft px-4 py-3 text-sm text-bad">
          <p className="min-w-0 flex-1 break-words">{loadError}</p>
          <button
            type="button"
            aria-label={l("关闭加载错误提示", "Dismiss loading error")}
            className="rounded-md px-2 py-1 text-base leading-none opacity-70 hover:bg-panel hover:opacity-100"
            onClick={() => setLoadError("")}
          >
            ×
          </button>
        </div>
      )}
      {notice && <p role="status" className="text-sm text-info">{notice}</p>}

      <section ref={resumeSectionRef} aria-labelledby="library-resumes-title">
        <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
          <h2 id="library-resumes-title" className="section-label">{l("简历", "Résumés")}</h2>
          {deepLink.returnTo && (
            <Link to={deepLink.returnTo} className="btn btn-sm">
              {l("返回岗位", "Back to role")}
            </Link>
          )}
        </div>
        <div
          ref={resumeUploadCardRef}
          id="library-resume-upload"
          tabIndex={-1}
          className="card mb-4 scroll-mt-24 p-5 outline-none focus:ring-2 focus:ring-info/40"
        >
          {/* Variant and binding first; name, file, and upload action second. */}
          <div className="mb-2 flex flex-col gap-2 sm:flex-row sm:items-center">
            <div className="segmented w-fit shrink-0">
              <button
                aria-pressed={bindingType === "general"}
                onClick={() => setBindingType("general")}
                className={`segmented-item ${bindingType === "general" ? "segmented-on" : ""}`}
              >
                {l("通用版", "General")}
              </button>
              <button
                aria-pressed={bindingType === "application"}
                onClick={() => setBindingType("application")}
                className={`segmented-item ${bindingType === "application" ? "segmented-on" : ""}`}
              >
                {l("岗位专属", "Role-specific")}
              </button>
            </div>
            {bindingType === "application" && (loading ? (
              <span role="status" className="flex-1 text-sm text-ink-3">{l("正在加载岗位…", "Loading roles…")}</span>
            ) : positions.length === 0 ? (
              <span className="flex-1 text-sm text-ink-3">
                {l("还没有岗位。先在「求职进展」中新增岗位，或让求职助手从 JD 中导入。", "No roles yet. Add one in Application Tracker or ask Career Assistant to import a job description.")}
              </span>
            ) : (
              <select
                aria-label={l("选择简历绑定岗位", "Select role for résumé")}
                value={resumeApplicationId ?? ""}
                onChange={(e) => setResumeApplicationId(e.target.value ? Number(e.target.value) : null)}
                className="input flex-1"
              >
                <option value="">{l("选择岗位", "Select a role")}</option>
                {positions.map((p) => (
                  <option key={p.id} value={p.id}>
                    {p.company}·{p.position}
                  </option>
                ))}
              </select>
            ))}
          </div>
          <div className="flex flex-col gap-2 sm:flex-row sm:items-center">
            <input
              aria-label={l("简历版本名", "Résumé version name")}
              value={resumeName}
              onChange={(e) => setResumeName(e.target.value)}
              placeholder={l("版本名称（留空则使用文件名）", "Version name (defaults to filename)")}
              className="input sm:w-52"
            />
            <input
              ref={resumeFileRef}
              type="file"
              accept=".pdf,.docx,.md,.txt"
              aria-label={l("选择简历文件", "Choose résumé file")}
              onChange={(e) => setResumeFile(e.target.files?.[0] ?? null)}
              className="flex-1 text-sm text-ink-2 file:mr-3 file:rounded-lg file:border-0 file:bg-panel-2 file:px-3 file:py-1.5 file:text-sm file:text-ink"
            />
            <button
              onClick={() => void uploadResume()}
              disabled={busy !== null || !resumeFile}
            className="btn-primary !px-5"
            >
              {busy === "resume" ? l("正在上传…", "Uploading…") : l("上传并解析", "Upload & Parse")}
            </button>
          </div>
          {busy === "resume" && (
            <p
              role="status"
              aria-live="polite"
              className="mt-3 flex items-center gap-2 rounded-lg bg-warn-soft px-3 py-2 text-sm text-warn"
            >
              <LoadingSpinner />
              {l("正在上传文件。提交完成后，后台会继续解析，你可以离开本页。", "Uploading file. After submission, parsing continues in the background and you may leave this page.")}
            </p>
          )}
          <p className="mt-2 text-xs text-ink-3">
            {l("支持 PDF / DOCX / Markdown / TXT。", "Supports PDF, DOCX, Markdown, and TXT.")}
          </p>
        </div>
        {activeOrFailedResumeJobs.length > 0 && (
          <div className="mb-4 space-y-2" aria-live="polite">
            {activeOrFailedResumeJobs.slice(0, 5).map((job) => (
              <ResumeJobStatus
                key={job.job_id}
                job={job}
                locale={locale}
                dismissing={Boolean(dismissingResumeJobs[job.job_id])}
                onDismiss={(item) => void dismissResumeJob(item)}
              />
            ))}
          </div>
        )}
        {completedResumeJobs.length > 0 && (
          <details className="mb-4 rounded-xl border border-line bg-panel px-3 py-2">
            <summary className="cursor-pointer text-sm font-medium text-ink-2">
              {l("最近完成", "Recently completed")} · {completedResumeJobs[0].name}
              {completedResumeJobs.length > 1 ? l(` 等 ${completedResumeJobs.length} 条`, ` and ${completedResumeJobs.length - 1} more`) : ""}
            </summary>
            <div className="mt-2 space-y-2">
              {completedResumeJobs.slice(0, 5).map((job) => (
                <ResumeJobStatus
                  key={job.job_id}
                  job={job}
                  locale={locale}
                  dismissing={Boolean(dismissingResumeJobs[job.job_id])}
                  onDismiss={(item) => void dismissResumeJob(item)}
                />
              ))}
            </div>
          </details>
        )}
        {/* Hidden file picker used for in-place résumé updates. */}
        <input
          ref={resumeUpdateRef}
          type="file"
          accept=".pdf,.docx,.md,.txt"
          onChange={(e) => void doResumeUpdate(e.target.files?.[0] ?? null)}
          className="hidden"
        />
        <div className="grid gap-3 md:grid-cols-2">
          {resumes.map((r) => {
            return (
              <div
                key={r.id}
                id={`resume-card-${r.id}`}
                data-resume-id={r.id}
                tabIndex={-1}
                className="card min-w-0 scroll-mt-24 p-4 outline-none focus:ring-2 focus:ring-info/40"
              >
                <div className="mb-1 flex min-w-0 items-center justify-between gap-2">
                  <span className="min-w-0 flex-1 break-words font-medium">{r.name}</span>
                  <span className="tag shrink-0 bg-panel-2 text-ink-2">
                    {r.binding === "application"
                      ? r.application_company && r.application_position
                        ? l(`岗位专属-${r.application_company}-${r.application_position}`, `Role-specific · ${r.application_company} · ${r.application_position}`)
                        : l("岗位专属-岗位已删除", "Role-specific · role deleted")
                      : l("通用版", "General")}
                  </span>
                </div>
                <div className="flex items-center justify-between gap-3">
                  <p className="min-w-0 flex-1 break-words text-xs tabular-nums text-ink-3">
                    {formatNumber(r.character_count, locale)} {l("字全文", "characters")}
                  </p>
                  <div className="flex shrink-0 items-center gap-2">
                    <button
                      onClick={() => void viewResumeText(r)}
                      disabled={viewingResumeId !== null}
                      className="btn btn-sm"
                    >
                      {viewingResumeId === r.id ? l("读取中…", "Loading…") : l("查看", "View")}
                    </button>
                    <button
                      onClick={() => pickResumeUpdate(r.id)}
                      disabled={updatingId !== null}
                      title={l("上传新文件替换这一版的内容（版本名与版本类型保持不变）", "Replace this version's content with a new file while keeping its name and type")}
                      className="btn btn-sm gap-1"
                    >
                      <RefreshIcon className="h-3 w-3" /> {updatingId === r.id ? l("更新中…", "Updating…") : l("更新", "Update")}
                    </button>
                    <button onClick={() => void removeResume(r)} className="btn btn-sm btn-danger">
                      {l("删除", "Delete")}
                    </button>
                  </div>
                </div>
              </div>
            );
          })}
          {loading
            ? <p role="status" className="text-sm text-ink-3">{l("正在加载简历…", "Loading résumés…")}</p>
            : resumesLoaded && resumes.length === 0 && <p className="text-sm text-ink-3">{l("还没有简历。上传一份 PDF、DOCX、Markdown 或 TXT 文件开始使用。", "No résumés yet. Upload a PDF, DOCX, Markdown, or TXT file to get started.")}</p>}
        </div>
      </section>
      {viewingResume && (
        <div
          className="fixed inset-0 z-50 flex items-center justify-center bg-black/35 p-4"
          role="dialog"
          aria-modal="true"
          aria-labelledby="resume-text-title"
          onMouseDown={(event) => {
            if (event.target === event.currentTarget && !savingResumeText) setViewingResume(null);
          }}
        >
          <div className="card flex max-h-[90vh] w-full max-w-3xl flex-col overflow-hidden p-0 shadow-xl">
            <div className="flex items-start justify-between gap-4 border-b border-line px-5 py-4">
              <div className="min-w-0">
                <h2 id="resume-text-title" className="break-words font-semibold">{viewingResume.name}</h2>
                <p className="mt-0.5 text-xs text-ink-3">{l("识别后的校正版文字", "Corrected extracted text")}</p>
              </div>
              <button
                ref={resumeTextCloseRef}
                type="button"
                aria-label={l("关闭简历文字", "Close résumé text")}
                className="btn btn-sm shrink-0"
                disabled={savingResumeText}
                onClick={() => setViewingResume(null)}
              >
                ×
              </button>
            </div>
            <p className="shrink-0 border-b border-line bg-panel-2 px-5 py-2 text-xs text-ink-3">
              {l("识别结果可能与原文件存在细微差别，请核对后按需修改。", "Extracted text may differ slightly from the original file. Review and correct it as needed.")}
            </p>
            {resumeTextError && (
              <p role="alert" className="shrink-0 bg-bad-soft px-5 py-2 text-xs text-bad">
                {resumeTextError}
              </p>
            )}
            <textarea
              aria-label={l("编辑简历校正版文字", "Edit corrected résumé text")}
              value={editedResumeText}
              onChange={(event) => setEditedResumeText(event.target.value)}
              disabled={savingResumeText}
              className="h-[60vh] min-h-64 flex-1 resize-none overflow-auto bg-transparent px-5 py-4 font-sans text-sm leading-6 text-ink outline-none"
              spellCheck={false}
            />
            <div className="flex items-center justify-between gap-3 border-t border-line px-5 py-3">
              <p className="text-xs tabular-nums text-ink-3">
                {formatNumber(editedResumeText.length, locale)} {l("字", "characters")}
              </p>
              <button
                type="button"
                className="btn-primary btn-sm"
                disabled={savingResumeText || editedResumeText === viewingResume.content_text || !editedResumeText.trim()}
                onClick={() => void saveResumeText()}
              >
                {savingResumeText ? l("保存中…", "Saving…") : l("保存修改", "Save changes")}
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}
