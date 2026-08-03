import { useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import {
  answerGrill, deleteGrillSession, deleteQuestionSet, finalizeGrillSession,
  generateSet, getGrillSessionSummary, getGrillSessions, getReadiness, resumeGrill,
  skipGrill, startGrill, suspendGrill,
} from "./grillApi";
import { generationErrorMessage, readinessErrorMessage } from "./grillGenerationError";
import { parseGrillDeepLink } from "./grillDeepLink";
import { presentReplayReview } from "./grillReplayPresentation";
import type { GrillFlowResponse, GrillQuestion, QuestionSetItem, ReadinessResponse, SessionListItem, SessionReplay } from "./grillContract";
import { currentOutputLocale } from "../../i18n/localePreference";
import { useLocalizer } from "../../i18n/useLocalizer";
import { HttpError } from "../../shared/api/transport";

type Phase = "home" | "practice" | "replay";

class GrillActionError extends Error {}

const MODEL_DISCLOSURE_DISMISSED_KEY = "careerdesk.grill.model-disclosure-dismissed";
const VERSION_NOTICE_DISMISSED_KEY = "careerdesk.grill.version-notice-dismissed";

function noticeWasDismissed(key: string): boolean {
  try {
    return window.localStorage.getItem(key) === "1";
  } catch {
    return false;
  }
}

function persistNoticeDismissal(key: string): void {
  try {
    window.localStorage.setItem(key, "1");
  } catch {
    // The in-memory state still hides the notice for the current page lifetime.
  }
}

function uuid() {
  return crypto.randomUUID();
}

function applicationResumeLibraryPath(applicationId: number): string {
  const returnTo = `/timeline?application=${applicationId}&tab=adaptation`;
  return `/library?${new URLSearchParams({ returnTo }).toString()}`;
}

function ReplayReview({ feedback, answerGuide }: {
  feedback: Record<string, unknown>;
  answerGuide: Record<string, unknown>;
}) {
  const l = useLocalizer();
  const review = presentReplayReview(feedback, answerGuide);
  const hasFeedback = review.strengths.length > 0 || review.gaps.length > 0 || review.nextStep;
  return <details><summary className="cursor-pointer text-sm">{l("查看点评与回答指南", "View feedback and answer guide")}</summary><div className="mt-3 space-y-4 rounded-xl bg-panel-2 p-4 text-sm">{hasFeedback && <section><h4 className="font-medium">{l("点评", "Feedback")}</h4>{review.strengths.length > 0 && <div className="mt-2"><p className="text-ink-3">{l("做得好的地方", "What worked well")}</p><ul className="mt-1 list-disc space-y-1 pl-5">{review.strengths.map((item, index) => <li key={`${index}:${item}`}>{item}</li>)}</ul></div>}{review.gaps.length > 0 && <div className="mt-3"><p className="text-ink-3">{l("可以改进", "What to improve")}</p><ul className="mt-1 list-disc space-y-1 pl-5">{review.gaps.map((item, index) => <li key={`${index}:${item}`}>{item}</li>)}</ul></div>}{review.nextStep && <div className="mt-3"><p className="text-ink-3">{l("下一步建议", "Recommended next step")}</p><p className="mt-1 whitespace-pre-wrap">{review.nextStep}</p></div>}</section>}{review.guideText && <section><h4 className="font-medium">{l("回答指南", "Answer guide")}</h4><p className="mt-2 whitespace-pre-wrap text-ink-3">{review.guideText}</p></section>}{!hasFeedback && !review.guideText && <p className="text-ink-3">{l("暂无点评或回答指南。", "No feedback or answer guide is available.")}</p>}</div></details>;
}

export function GrillPage() {
  const l = useLocalizer();
  const message = (error: unknown) => error instanceof HttpError || error instanceof GrillActionError
    ? error.message
    : l("操作失败，请重试", "Something went wrong. Try again.");
  const category: Record<string, string> = {
    hr_motivation: l("动机与匹配", "Motivation & fit"), resume_deep_dive: l("简历深挖", "Résumé deep dive"),
    behavioral_situational: l("行为与情境", "Behavioral & situational"), professional_domain: l("专业领域", "Professional expertise"),
    business_company: l("业务与公司", "Business & company"), case_work_sample: l("案例与作业", "Case & work sample"),
  };
  const verdict: Record<string, string> = {
    meets: l("达到要求", "Meets expectations"), partially_meets: l("部分达到", "Partially meets"), needs_work: l("需要加强", "Needs work"),
    ungradable: l("无法判定", "Could not assess"), skipped: l("已跳过", "Skipped"),
  };
  const location = useLocation();
  const deepLink = useMemo(() => parseGrillDeepLink(location.search), [location.search]);
  const handledApplicationLinkRef = useRef<string | null>(null);
  const handledSessionLinkRef = useRef<string | null>(null);
  const [data, setData] = useState<ReadinessResponse | null>(null);
  const [edition, setEdition] = useState<"basic" | "custom">("basic");
  const [resumeId, setResumeId] = useState<number | null>(null);
  const [applicationId, setApplicationId] = useState<number | null>(null);
  const [refreshIntent, setRefreshIntent] = useState(false);
  const [selection, setSelection] = useState<ReadinessResponse["selection"]>(undefined);
  const [countInput, setCountInput] = useState("10");
  const [sessions, setSessions] = useState<SessionListItem[]>([]);
  const [sessionsLoaded, setSessionsLoaded] = useState(false);
  const [phase, setPhase] = useState<Phase>("home");
  const [sessionId, setSessionId] = useState<number | null>(null);
  const [question, setQuestion] = useState<GrillQuestion | null>(null);
  const [progress, setProgress] = useState({ answered: 0, total: 0 });
  const [followUp, setFollowUp] = useState<string | null>(null);
  const [answer, setAnswer] = useState("");
  const [replay, setReplay] = useState<SessionReplay | null>(null);
  const [busy, setBusy] = useState(false);
  const [retryingSetId, setRetryingSetId] = useState<number | null>(null);
  const [error, setError] = useState("");
  const [highlightedSessionId, setHighlightedSessionId] = useState<number | null>(null);
  const [showModelDisclosure, setShowModelDisclosure] = useState(() => !noticeWasDismissed(MODEL_DISCLOSURE_DISMISSED_KEY));
  const [showVersionNotice, setShowVersionNotice] = useState(() => !noticeWasDismissed(VERSION_NOTICE_DISMISSED_KEY));

  function dismissModelDisclosure() {
    setShowModelDisclosure(false);
    persistNoticeDismissal(MODEL_DISCLOSURE_DISMISSED_KEY);
  }

  function dismissVersionNotice() {
    setShowVersionNotice(false);
    persistNoticeDismissal(VERSION_NOTICE_DISMISSED_KEY);
  }

  const applications = useMemo(
    () => Object.values(data?.applications.columns ?? {}).flat(), [data],
  );
  const readySets = (data?.question_sets ?? []).filter((item) => item.state === "ready" && !item.archived_at && ["current", "fixed"].includes(item.currentness));
  const staleSets = (data?.question_sets ?? []).filter((item) => item.state === "ready" && !item.archived_at && ["stale", "legacy"].includes(item.currentness));
  const failedSets = (data?.question_sets ?? []).filter((item) => item.state === "failed" && !item.archived_at);
  const preparingSets = (data?.question_sets ?? []).filter((item) => ["pending", "running"].includes(item.state));
  const maximumAvailableQuestionCount = readySets.reduce(
    (maximum, item) => Math.max(maximum, item.question_count), 0,
  );
  const hasCurrentSetForSelection = readySets.some((item) => (
    item.edition === edition
    && (edition === "basic" ? item.resume_id === resumeId : item.application_id === applicationId)
  ));

  function sessionQuestionCount(set: QuestionSetItem): number {
    const parsed = Number.parseInt(countInput, 10);
    const requested = Number.isFinite(parsed) ? parsed : 1;
    const available = Math.min(set.question_count, set.unpracticed_count || set.question_count);
    return Math.min(Math.max(1, requested), available);
  }

  function normalizeCountInput() {
    const parsed = Number.parseInt(countInput, 10);
    const maximum = Math.max(1, maximumAvailableQuestionCount);
    setCountInput(String(Math.min(maximum, Math.max(1, Number.isFinite(parsed) ? parsed : 1))));
  }

  async function reload() {
    const [next, active] = await Promise.all([
      getReadiness(), getGrillSessions("active,suspended"),
    ]);
    setData(next);
    setSessions(active);
    setSessionsLoaded(true);
    setResumeId((current) => current ?? next.resumes.find((item) => !item.archived)?.id ?? null);
    setApplicationId((current) => current ?? Object.values(next.applications.columns).flat()[0]?.id ?? null);
  }

  useEffect(() => { reload().catch((reason) => setError(message(reason))); }, []);

  useEffect(() => {
    if (deepLink.applicationId === null || data === null) return;
    const exists = Object.values(data.applications.columns).flat()
      .some((item) => item.id === deepLink.applicationId);
    if (!exists) return;
    const key = `${location.key}:${deepLink.applicationId}`;
    if (handledApplicationLinkRef.current === key) return;
    handledApplicationLinkRef.current = key;
    setEdition("custom");
    setApplicationId(deepLink.applicationId);
    setRefreshIntent(false);
  }, [data, deepLink.applicationId, location.key]);

  useEffect(() => {
    if (deepLink.sessionId === null || data === null || !sessionsLoaded) return;
    const key = `${location.key}:${deepLink.sessionId}`;
    if (handledSessionLinkRef.current === key) return;
    handledSessionLinkRef.current = key;
    const resumable = sessions.some((item) => item.id === deepLink.sessionId);
    if (resumable) {
      setHighlightedSessionId(deepLink.sessionId);
      window.setTimeout(() => {
        document.getElementById(`grill-session-${deepLink.sessionId}`)
          ?.scrollIntoView({ block: "center", behavior: "smooth" });
      }, 0);
      return;
    }
    getGrillSessionSummary(deepLink.sessionId)
      .then((value) => {
        if (value.status === "error") throw new GrillActionError(l("练习场次不存在或已结束", "This practice session no longer exists or has ended."));
        setReplay(value);
        setPhase("replay");
      })
      .catch((reason) => setError(message(reason)));
  }, [data, deepLink.sessionId, location.key, sessions, sessionsLoaded]);

  useEffect(() => {
    if (preparingSets.length === 0) return;
    const timer = window.setInterval(() => {
      reload().catch((reason) => setError(message(reason)));
    }, 1500);
    return () => window.clearInterval(timer);
  }, [preparingSets.length]);

  useEffect(() => {
    const selectedId = edition === "basic" ? resumeId : applicationId;
    if (!selectedId) {
      setSelection(undefined);
      return;
    }
    let active = true;
    const query = new URLSearchParams({ edition });
    query.set(edition === "basic" ? "resume_id" : "application_id", String(selectedId));
    getReadiness(query.toString())
      .then((value) => {
        if (!active) return;
        setSelection(value.selection);
      })
      .catch(() => {
        if (!active) return;
        setSelection(undefined);
      });
    return () => { active = false; };
  }, [edition, resumeId, applicationId]);

  async function generate() {
    setBusy(true); setError("");
    try {
      const body = edition === "basic"
        ? { edition, resume_id: resumeId, client_command_id: uuid(), output_locale: currentOutputLocale(), refresh: refreshIntent || hasCurrentSetForSelection }
        : { edition, application_id: applicationId, client_command_id: uuid(), output_locale: currentOutputLocale(), refresh: refreshIntent || hasCurrentSetForSelection };
      const result = await generateSet(body);
      if (result.status === "error") throw new GrillActionError(generationErrorMessage(result.code, result.message, currentOutputLocale()));
      setRefreshIntent(false);
      await reload();
    } catch (reason) { setError(message(reason)); } finally { setBusy(false); }
  }

  async function retryGeneration(set: QuestionSetItem) {
    if (!set.edition) return;
    setBusy(true); setRetryingSetId(set.id); setError("");
    try {
      const body = set.edition === "basic"
        ? { edition: set.edition, resume_id: set.resume_id, client_command_id: uuid(), output_locale: currentOutputLocale(), refresh: true }
        : { edition: set.edition, application_id: set.application_id, client_command_id: uuid(), output_locale: currentOutputLocale(), refresh: true };
      const result = await generateSet(body);
      if (result.status === "error") throw new GrillActionError(generationErrorMessage(result.code, result.message, currentOutputLocale()));
      await deleteQuestionSet(set.id);
      await reload();
    } catch (reason) { setError(message(reason)); }
    finally { setBusy(false); setRetryingSetId(null); }
  }

  const selectionReady = Boolean(selection?.ready);
  const capacityLabel = selection?.capacity?.state === "direct"
    ? null
    : selection?.capacity?.state === "compressed"
      ? l(`材料将先做可追溯压缩（额外 ${selection.capacity.extra_calls} 次调用），最多生成 ${selection.capacity.effective_question_limit} 题`, `The source material will be compressed with traceability (${selection.capacity.extra_calls} extra model ${selection.capacity.extra_calls === 1 ? "call" : "calls"}); up to ${selection.capacity.effective_question_limit} questions can be generated.`)
      : selection?.capacity?.state === "blocked"
        ? selection.capacity.code === "model_capacity_required"
          ? data?.model_configured
            ? l("当前模型缺少可信容量信息，请在模型设置中补充上下文和最大输出上限", "The current model is missing reliable capacity data. Add its context and maximum-output limits in model settings.")
            : null
          : l("当前模型上下文不足，请改用更大上下文模型或精简材料", "The current model cannot fit this material. Choose a model with a larger context window or shorten the source material.")
        : null;

  function consume(result: GrillFlowResponse) {
    if (result.status === "error") throw new GrillActionError(l("练习操作失败，请重试", "The practice action failed. Retry."));
    if (result.progress) setProgress(result.progress);
    if (result.session_id) setSessionId(result.session_id);
    if (result.status === "processing") {
      setBusy(true);
      const target = result.session_id ?? sessionId;
      if (target) window.setTimeout(() => {
        resumeGrill(target).then(consume).catch((reason) => { setBusy(false); setError(message(reason)); });
      }, 700);
      return;
    }
    setBusy(false);
    if (result.status === "finished") {
      if (!result.session_id && !sessionId) return;
      finalizeGrillSession(result.session_id ?? sessionId!).then((value) => {
        setReplay(value); setPhase("replay"); reload().catch(() => undefined);
      }).catch((reason) => setError(message(reason)));
      return;
    }
    if (result.status === "suspended") {
      setPhase("home"); setQuestion(null); reload().catch(() => undefined); return;
    }
    if (result.follow_up) { setFollowUp(result.follow_up); setAnswer(""); return; }
    setFollowUp(null); setQuestion(result.question ?? null); setAnswer(""); setPhase("practice");
  }

  async function begin(set: QuestionSetItem) {
    setBusy(true); setError("");
    try { consume(await startGrill(set.id, sessionQuestionCount(set))); }
    catch (reason) { setError(message(reason)); } finally { setBusy(false); }
  }

  function prepareRegeneration(set: QuestionSetItem) {
    if (!set.edition) return;
    setEdition(set.edition);
    setResumeId(set.edition === "basic" ? set.resume_id : null);
    setApplicationId(set.edition === "custom" ? set.application_id : null);
    setRefreshIntent(true);
    window.scrollTo({ top: 0, behavior: "smooth" });
  }

  async function removeSet(set: QuestionSetItem) {
    if (!window.confirm(l("要删除这个题集吗？如果已有练习记录，题集将改为归档并保留历史。", "Delete this question set? If it has practice history, it will be archived and the history will be kept."))) return;
    setBusy(true); setError("");
    try { await deleteQuestionSet(set.id); await reload(); }
    catch (reason) { setError(message(reason)); }
    finally { setBusy(false); }
  }

  async function resume(item: SessionListItem) {
    setBusy(true); setError("");
    try { setSessionId(item.id); consume(await resumeGrill(item.id)); }
    catch (reason) { setError(message(reason)); } finally { setBusy(false); }
  }

  async function submit() {
    if (!sessionId || !question || !answer.trim()) return;
    setBusy(true); setError("");
    try { consume(await answerGrill(sessionId, question.id, answer.trim(), Boolean(followUp))); }
    catch (reason) { setBusy(false); setError(message(reason)); }
  }

  async function skip() {
    if (!sessionId || !question) return;
    setBusy(true); setError("");
    try { consume(await skipGrill(sessionId, question.id)); }
    catch (reason) { setError(message(reason)); } finally { setBusy(false); }
  }

  if (phase === "practice" && question) return (
    <section className="mx-auto max-w-3xl space-y-5">
      <div className="flex items-center justify-between text-sm text-ink-3">
        <span>{progress.answered} / {progress.total}</span>
        <button className="btn-secondary" disabled={busy} onClick={() => sessionId && suspendGrill(sessionId).then(consume)}>{l("稍后继续", "Continue later")}</button>
      </div>
      <div className="card space-y-3 p-6">
        <div className="flex gap-2 text-xs text-ink-3"><span>{category[question.category] ?? question.category}</span><span>·</span><span>{question.channel === "written" ? l("笔试", "Written") : l("面试", "Interview")}</span></div>
        <h2 className="text-xl font-semibold">{question.text}</h2>
        {followUp && <div className="rounded-xl bg-accent-soft p-4"><p className="text-xs text-ink-3">{l("追问", "Follow-up")}</p><p className="mt-1">{followUp}</p></div>}
        <p className="rounded-lg bg-panel-2 p-3 text-xs text-ink-3">{l("提交后，题目、评分标准、你的回答和本题追问记录会发送给已配置的模型进行评估。", "When you submit, the question, evaluation criteria, your answer, and follow-up history are sent to your configured model for assessment.")}</p>
        <textarea className="input min-h-40 w-full" value={answer} onChange={(event) => setAnswer(event.target.value)} placeholder={question.channel === "written" ? l("写下完整答案…", "Write your complete answer…") : l("按口述方式回答…", "Answer as you would in conversation…")} />
        {error && <p className="text-sm text-bad">{error}</p>}
        <div className="flex justify-between"><button className="btn-ghost" disabled={busy} onClick={skip}>{l("暂时跳过", "Skip for now")}</button><button className="btn-primary" disabled={busy || !answer.trim()} onClick={submit}>{busy ? l("正在评估…", "Evaluating…") : l("提交回答", "Submit answer")}</button></div>
      </div>
    </section>
  );

  if (phase === "replay" && replay) return (
    <section className="mx-auto max-w-4xl space-y-4">
      <div className="flex flex-col items-start gap-3 sm:flex-row sm:items-center sm:justify-between"><div className="min-w-0"><h2 className="break-words text-xl font-semibold">{replay.context_label ?? l("练习复盘", "Practice review")}</h2><p className="text-sm text-ink-3">{l("回答指南用于复盘，不是唯一标准答案。", "Answer guides support reflection; they are not the only valid answers.")}</p></div><button className="btn-secondary shrink-0" onClick={() => { setPhase("home"); setReplay(null); }}>{l("返回", "Back")}</button></div>
      {(replay.answers ?? []).map((item) => <article className="card space-y-3 p-5" key={item.session_item_id}><div className="flex flex-wrap items-start justify-between gap-3"><h3 className="min-w-0 flex-1 break-words font-medium">{item.text}</h3><span className="badge shrink-0">{verdict[item.verdict]}</span></div><p className="break-words text-sm text-ink-3">{l("能力：", "Competency: ")}{item.primary_competency}</p><ReplayReview feedback={item.feedback} answerGuide={item.answer_guide} /></article>)}
    </section>
  );

  return (
    <section className="space-y-6">
      <header><h2 className="text-xl font-semibold">{l("开始练习", "Practice")}</h2><p className="mt-1 text-sm text-ink-3">{l("先生成题集，再从下方选择一组题目开始练习。", "Generate a question set, then choose one below to begin a practice session.")}</p></header>
      {error && <div className="rounded-xl bg-bad-soft p-3 text-sm text-bad">{error}</div>}
      <div className="card space-y-4 p-5">
        <div><h3 className="font-semibold">{l("生成题集", "Generate a question set")}</h3></div>
        <div className="grid gap-3 sm:grid-cols-2" role="group" aria-label={l("选择练习方式", "Choose a practice type")}>
          <button aria-pressed={edition === "basic"} className={`button-wrap min-w-0 cursor-pointer rounded-xl border p-4 text-left transition ${edition === "basic" ? "border-accent bg-accent-soft ring-1 ring-accent" : "border-line bg-panel-2 hover:border-line-strong hover:bg-panel"}`} onClick={() => { setEdition("basic"); setRefreshIntent(false); }}><span className="font-semibold">{l("通用练习", "General practice")}</span><span className="mt-1 block text-sm leading-relaxed text-ink-3">{l("基于你的简历生成常见的面试问题。", "Generate common interview questions from your résumé.")}</span></button>
          <button aria-pressed={edition === "custom"} className={`button-wrap min-w-0 cursor-pointer rounded-xl border p-4 text-left transition ${edition === "custom" ? "border-accent bg-accent-soft ring-1 ring-accent" : "border-line bg-panel-2 hover:border-line-strong hover:bg-panel"}`} onClick={() => { setEdition("custom"); setRefreshIntent(false); }}><span className="font-semibold">{l("岗位定制", "Role-specific")}</span><span className="mt-1 block text-sm leading-relaxed text-ink-3">{l("结合具体岗位和简历，生成有针对性的面试问题。", "Create targeted questions from a specific role and résumé.")}</span></button>
        </div>
        <div className="space-y-4 border-t border-line pt-4">
        <div><p className="font-medium">{edition === "basic" ? l("通用练习设置", "General-practice settings") : l("岗位定制设置", "Role-specific settings")}</p><p className="mt-1 text-xs text-ink-3">{edition === "basic" ? l("选择用于出题的简历。", "Choose the résumé to use.") : l("选择岗位，并确保已绑定简历和岗位描述。", "Choose a role with a résumé and job description attached.")}</p></div>
        {edition === "basic" ? (
          <div className="space-y-3"><label className="block text-sm">{l("简历", "Résumé")}
            <select className="input mt-2 w-full" value={resumeId ?? ""} onChange={(event) => { setResumeId(Number(event.target.value) || null); setRefreshIntent(false); }}>
              <option value="">{l("请选择", "Select one")}</option>
              {data?.resumes.filter((item) => !item.archived).map((item) => <option value={item.id} key={item.id}>{item.name}</option>)}
            </select>
          </label>{data && data.resumes.every((item) => item.archived) && <Link className="btn-secondary w-fit" to="/library">{l("去简历库添加简历", "Add a résumé in the library")}</Link>}</div>
        ) : <>
          <label className="block text-sm">{l("岗位", "Role")}
            <select className="input mt-2 w-full" value={applicationId ?? ""} onChange={(event) => {
              const id = Number(event.target.value) || null;
              setApplicationId(id);
              setRefreshIntent(false);
            }}>
              <option value="">{l("请选择", "Select one")}</option>
              {applications.map((item) => <option value={item.id} key={item.id}>{item.company} · {item.position}</option>)}
            </select>
          </label>
          <div className="grid gap-2 rounded-xl bg-panel-2 p-3 text-sm sm:grid-cols-2">
            <span>{l("简历：", "Résumé: ")}{selection?.requirements?.resume?.ready ? selection.requirements.resume.label ?? l("已准备", "Ready") : l("尚未绑定", "Not attached")}</span>
            <span>{l("岗位描述：", "Job description: ")}{selection?.requirements?.jd?.present ? l("已识别", "Available") : l("尚未添加", "Not added")}</span>
          </div>
          <div className="flex flex-wrap gap-2">
            {applicationId && selection && !selection.requirements?.resume?.ready && <Link className="btn-secondary w-fit" to={applicationResumeLibraryPath(applicationId)}>{l("去简历库添加该岗位的简历", "Attach a résumé for this role")}</Link>}
            {applicationId && selection && !selection.requirements?.jd?.present && <Link className="btn-secondary w-fit" to={`/timeline?application=${applicationId}`}>{l("去岗位详情，点击“编辑”后补充岗位描述", "Open role details and choose Edit to add the job description")}</Link>}
          </div>
        </>}
        {showModelDisclosure && <div className="flex items-start gap-3 rounded-lg bg-panel-2 p-3 text-xs text-ink-3"><p className="min-w-0 flex-1">{l("生成时会将所选简历全文发送给已配置的模型；岗位定制还会发送完整岗位描述。", "Generation sends the full selected résumé to your configured model. Role-specific sets also send the full job description.")}</p><button type="button" aria-label={l("关闭模型发送提示", "Dismiss model-data notice")} title={l("关闭", "Dismiss")} className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded text-base leading-none transition-colors hover:bg-black/5 hover:text-ink" onClick={dismissModelDisclosure}>×</button></div>}
        {capacityLabel && <p className={`text-sm ${selection?.capacity?.state === "blocked" ? "text-bad" : "text-ink-3"}`}>{capacityLabel}</p>}
        {selection?.code && !selectionReady && <p className="text-sm text-bad">{readinessErrorMessage(selection.code, currentOutputLocale())}</p>}
        <button type="button" className="btn-primary" disabled={busy || !data?.model_configured || !selectionReady} onClick={generate}>{busy ? l("正在生成…", "Generating…") : refreshIntent || hasCurrentSetForSelection ? l("生成新题集", "Generate new set") : l("生成题集", "Generate set")}</button>
        <p className="text-xs text-ink-3">{l("生成通常需要 2–5 分钟，材料较长时可能更久；可以离开此页面，完成后会自动显示。", "Generation usually takes 2–5 minutes and may take longer for large source material. You may leave this page; the result appears automatically when ready.")}</p>
        {!data?.model_configured && <p className="text-sm text-warn">{l("请先在“模型与隐私”中配置模型。", "Configure a model in Model & Privacy first.")}</p>}
        </div>
      </div>
      {preparingSets.length > 0 && <div className="space-y-3"><h3 className="font-semibold">{l("正在准备题集", "Preparing question sets")}</h3>{preparingSets.map((item) => <article className="card p-4" key={item.id}><div className="flex flex-wrap items-center justify-between gap-2"><div><p className="font-medium">{item.context_label}</p><p className="mt-1 text-sm text-ink-3">{item.stage === "summarizing" ? l("正在压缩材料", "Compressing source material") : item.stage === "generating" ? l("正在生成题目", "Generating questions") : l("正在确认材料范围", "Checking source scope")} · {l("预计 2–5 分钟，材料较长时可能更久", "Usually 2–5 minutes; large source material may take longer")}</p></div><span className="badge">{l("后台处理中", "Processing in background")}</span></div></article>)}</div>}
      {failedSets.length > 0 && <div className="space-y-3"><h3 className="font-semibold">{l("生成失败", "Generation failed")}</h3>{failedSets.map((item) => <article className="card flex flex-col items-start gap-3 p-4 sm:flex-row sm:items-center sm:justify-between" key={item.id}><div className="min-w-0"><p className="break-words font-medium">{item.context_label}</p><p className="break-words text-sm text-bad">{generationErrorMessage(item.safe_error_code, undefined, currentOutputLocale())}</p></div><div className="flex flex-wrap gap-2"><button type="button" className="btn-secondary" disabled={busy} aria-busy={retryingSetId === item.id} onClick={() => void retryGeneration(item)}>{retryingSetId === item.id ? l("正在重试…", "Retrying…") : l("重试", "Retry")}</button><button type="button" className="btn-ghost text-bad" disabled={busy} onClick={() => removeSet(item)}>{l("删除", "Delete")}</button></div></article>)}</div>}
      {staleSets.length > 0 && <div className="space-y-3"><h3 className="font-semibold">{l("材料已更新", "Source material updated")}</h3>{staleSets.map((item) => <article className="card flex flex-col items-start gap-3 p-4 sm:flex-row sm:items-center sm:justify-between" key={item.id}><div className="min-w-0"><p className="break-words font-medium">{item.context_label}</p><p className="text-sm text-ink-3">{l("旧题集会保留练习记录，但不能再开始新的练习。", "The old set keeps its practice history but cannot start new sessions.")}</p></div><div className="flex flex-wrap gap-2"><button className="btn-primary" onClick={() => prepareRegeneration(item)}>{l("使用新材料生成", "Generate from new material")}</button><button className="btn-ghost text-bad" onClick={() => removeSet(item)}>{l("归档或删除", "Archive or delete")}</button></div></article>)}</div>}
      <div className="space-y-3"><div className="flex flex-wrap items-center justify-between gap-3"><h3 className="font-semibold">{l("可用题集", "Available question sets")}</h3><label className="flex items-center gap-2 text-sm">{l("每次题数", "Questions per session")} <input type="number" inputMode="numeric" min={1} max={maximumAvailableQuestionCount || undefined} disabled={maximumAvailableQuestionCount === 0} className="input w-20" value={countInput} onChange={(event) => { if (/^\d*$/.test(event.target.value)) setCountInput(event.target.value); }} onBlur={normalizeCountInput} /></label></div>{showVersionNotice && <div className="flex items-start gap-3 rounded-lg bg-panel-2 p-3 text-sm text-ink-3"><p className="min-w-0 flex-1">{l("材料更新后可生成新版本，已有题集和练习记录不会改变。", "When source material changes, you can generate a new version without changing existing sets or practice history.")}</p><button type="button" aria-label={l("关闭题集版本提示", "Dismiss version notice")} title={l("关闭", "Dismiss")} className="inline-flex h-5 w-5 shrink-0 items-center justify-center rounded text-base leading-none transition-colors hover:bg-black/5 hover:text-ink" onClick={dismissVersionNotice}>×</button></div>}</div>
      <div className="grid gap-3 md:grid-cols-2">{readySets.map((item) => <article className="card p-4" key={item.id}><div className="flex flex-col items-start gap-3 sm:flex-row sm:justify-between"><div className="min-w-0 flex-1"><h4 className="break-words font-medium">{item.context_label}</h4><p className="mt-1 text-sm text-ink-3">{item.edition === "custom" ? l("岗位定制", "Role-specific") : l("通用练习", "General practice")} · {l(`共 ${item.question_count} 题`, `${item.question_count} ${item.question_count === 1 ? "question" : "questions"}`)} · {l(`${item.unpracticed_count} 题未练`, `${item.unpracticed_count} unpracticed`)} · {l("材料为最新版本", "Source material is current")}</p></div><div className="flex flex-wrap gap-2 sm:justify-end">{item.unpracticed_count === 0 ? <button className="btn-primary" disabled={busy} onClick={() => prepareRegeneration(item)}>{l("生成新题集", "Generate new set")}</button> : <button className="btn-primary" disabled={busy} onClick={() => begin(item)}>{l(`开始 ${sessionQuestionCount(item)} 题`, `Start ${sessionQuestionCount(item)} ${sessionQuestionCount(item) === 1 ? "question" : "questions"}`)}</button>}{item.unpracticed_count > 0 && <button className="btn-secondary" disabled={busy} onClick={() => prepareRegeneration(item)}>{l("重新生成", "Regenerate")}</button>}<button className="btn-ghost text-bad" disabled={busy} onClick={() => removeSet(item)}>{l("删除", "Delete")}</button></div></div></article>)}</div>
      {sessions.length > 0 && <div className="space-y-3"><h3 className="font-semibold">{l("继续上次练习", "Continue a previous session")}</h3>{sessions.map((item) => <div id={`grill-session-${item.id}`} className={`card flex flex-col items-start gap-3 p-4 sm:flex-row sm:items-center sm:justify-between ${highlightedSessionId === item.id ? "ring-2 ring-accent" : ""}`} key={item.id}><div className="min-w-0"><p className="break-words font-medium">{item.context_label}</p><p className="text-sm text-ink-3">{l(`已完成 ${item.answered} / ${item.total} 题`, `${item.answered} of ${item.total} completed`)}{highlightedSessionId === item.id ? l(" · 求职助手已定位到这里", " · Career Assistant brought you here") : ""}</p></div><div className="flex flex-wrap gap-2"><button className="btn-secondary" onClick={() => resume(item)}>{l("继续练习", "Continue")}</button><button className="btn-ghost text-bad" onClick={() => deleteGrillSession(item.id).then(reload)}>{l("删除", "Delete")}</button></div></div>)}</div>}
    </section>
  );
}
