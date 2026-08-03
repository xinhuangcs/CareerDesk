import { useEffect, useRef, useState, type FormEvent, type KeyboardEvent } from "react";
import { postJson } from "../../shared/api/transport";
import { ApplicationDetail, type ApplicationStage } from "./timelineContract";
import { limitCodePoints, stageEndsApplication } from "./timelineInteractionState";
import {
  COLUMNS,
  localizedColumns,
  nextStageAfterCurrentStageChange,
} from "./timelineDisplay";
import { useLocale } from "../../i18n/localePreference";
import { useLocalizer } from "../../i18n/useLocalizer";

export function TimelineCreateApplicationDialog({
  onCancel,
  onCreated,
}: {
  onCancel: () => void;
  onCreated: (application: ApplicationDetail) => void | Promise<void>;
}) {
  const l = useLocalizer();
  const { locale } = useLocale();
  const columns = localizedColumns(locale);
  const [company, setCompany] = useState("");
  const [position, setPosition] = useState("");
  const [department, setDepartment] = useState("");
  const [channel, setChannel] = useState("");
  const [stage, setStage] = useState<ApplicationStage>("backlog");
  const [currentStep, setCurrentStep] = useState("");
  const [appliedDate, setAppliedDate] = useState("");
  const [pauseReason, setPauseReason] = useState("");
  const [nextStage, setNextStage] = useState<ApplicationStage>("backlog");
  const [nextStageManuallyEdited, setNextStageManuallyEdited] = useState(false);
  const [nextStep, setNextStep] = useState("");
  const [nextDate, setNextDate] = useState("");
  const [nextTime, setNextTime] = useState("");
  const [nextNote, setNextNote] = useState("");
  const [jdText, setJdText] = useState("");
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const companyRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    const previousFocus = document.activeElement instanceof HTMLElement
      ? document.activeElement
      : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    companyRef.current?.focus();
    return () => {
      document.body.style.overflow = previousOverflow;
      previousFocus?.focus();
    };
  }, []);

  function handleDialogKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape" && !saving) {
      event.preventDefault();
      onCancel();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>(
      "button:not([disabled]), input:not([disabled]), select:not([disabled]), textarea:not([disabled])",
    ) ?? []);
    if (focusable.length === 0) return;
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!company.trim() || !position.trim() || saving) return;
    const hasNextActionInput = !stageEndsApplication(stage) && Boolean(
      nextStep.trim() || nextDate || nextTime || nextNote.trim() || nextStage !== stage,
    );
    if (hasNextActionInput && !nextStep.trim()) {
      setError(l("请先填写下一步；日期、时间、说明和完成后阶段都属于这项安排。", "Enter the next step first; its date, time, note, and resulting stage all belong to that action."));
      return;
    }
    if (!stageEndsApplication(stage) && nextStep.trim() && nextTime && !nextDate) {
      setError(l("设置下一步具体时间前，请先选择日期。", "Choose a date before setting a time for the next step."));
      return;
    }
    setSaving(true);
    setError("");
    try {
      const created = ApplicationDetail.parse(await postJson<unknown>("/api/timeline/applications", {
        company,
        position,
        department: department || null,
        channel: channel || null,
        stage,
        current_step: currentStep.trim() || null,
        applied_date: appliedDate || null,
        pause_reason: stage === "pooled" ? pauseReason.trim() || null : null,
        next_action: nextStep.trim() && !stageEndsApplication(stage) ? {
          stage: nextStage,
          step: nextStep.trim(),
          date: nextDate || null,
          time: nextTime || null,
          note: nextNote.trim() || null,
        } : null,
        jd_text: jdText || null,
      }));
      await onCreated(created);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : l("新增岗位失败，请稍后重试", "Could not add the role. Try again later."));
      setSaving(false);
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" role="presentation">
      <button
        type="button"
        tabIndex={-1}
        aria-label={l("取消新增岗位", "Cancel adding role")}
        onClick={() => { if (!saving) onCancel(); }}
        className="absolute inset-0 cursor-default bg-black/40"
      />
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="create-application-title"
        onKeyDown={handleDialogKeyDown}
        className="relative z-10 max-h-[90vh] w-full max-w-xl overflow-y-auto rounded-2xl border border-line bg-panel p-5 outline-none"
        style={{ boxShadow: "var(--shadow-pop)" }}
      >
        <div className="mb-4 flex items-center justify-between gap-3">
          <div>
            <h2 id="create-application-title" className="text-base font-semibold">{l("新增岗位", "Add role")}</h2>
            <p className="mt-1 text-xs text-ink-3">{l("先填写已知信息即可，之后可以随时补充或修改。", "Start with what you know. You can add or change details at any time.")}</p>
          </div>
          <button
            type="button"
            onClick={onCancel}
            disabled={saving}
            aria-label={l("关闭新增岗位", "Close add-role dialog")}
            className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-lg leading-none text-ink-3 transition-colors hover:bg-panel-2 hover:text-ink disabled:cursor-not-allowed disabled:opacity-50"
          >
            ×
          </button>
        </div>
        <form onSubmit={(event) => void submit(event)}>
          <fieldset disabled={saving} className="flex flex-col gap-3 disabled:opacity-70">
          <div className="grid gap-3 sm:grid-cols-2">
            <label className="text-sm">
              <span className="font-medium">{l("公司名称", "Company")} <span className="text-bad">*</span></span>
              <input ref={companyRef} value={company} onChange={(event) => setCompany(limitCodePoints(event.target.value, 200))} required className="input mt-1 w-full" />
            </label>
            <label className="text-sm">
              <span className="font-medium">{l("岗位名称", "Role title")} <span className="text-bad">*</span></span>
              <input value={position} onChange={(event) => setPosition(limitCodePoints(event.target.value, 300))} required className="input mt-1 w-full" />
            </label>
            <label className="text-sm">
              <span className="font-medium">{l("部门", "Department")}</span>
              <input value={department} onChange={(event) => setDepartment(limitCodePoints(event.target.value, 200))} className="input mt-1 w-full" />
            </label>
            <label className="text-sm">
              <span className="font-medium">{l("渠道", "Source")}</span>
              <input value={channel} onChange={(event) => setChannel(limitCodePoints(event.target.value, 100))} placeholder={l("官网 / 内推 / BOSS…", "Company site / Referral / LinkedIn…")} className="input mt-1 w-full" />
            </label>
          </div>
          <label className="text-sm">
            <span className="font-medium">{l("当前阶段", "Current stage")}</span>
            <select value={stage} onChange={(event) => {
              const next = event.target.value as (typeof COLUMNS)[number][0];
              setStage(next);
              setNextStage((current) => nextStageAfterCurrentStageChange(
                current,
                next,
                nextStageManuallyEdited,
              ));
            }} className="input mt-1 w-full">
              {columns.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
            </select>
          </label>
          <label className="text-sm">
            <span className="font-medium">{l("当前环节（可选）", "Current step (optional)")}</span>
            <input
              value={currentStep}
              onChange={(event) => setCurrentStep(limitCodePoints(event.target.value, 300))}
              placeholder={l("最近已经确认到达的环节，例如：一面", "Most recently confirmed step, e.g. first interview")}
              className="input mt-1 w-full"
            />
          </label>
          <label className="text-sm">
            <span className="font-medium">{l("投递日期（可选）", "Application date (optional)")}</span>
            <input
              type="date"
              value={appliedDate}
              onChange={(event) => setAppliedDate(event.target.value)}
              className="input mt-1 w-full"
            />
            {stage === "applied" && !appliedDate && (
              <span className="mt-1 block text-xs text-ink-3">
                {l("当前阶段为“已投递”，留空会按产品时区自动记为今天。", "The current stage is Applied. If left blank, today's date is recorded in the app timezone.")}
              </span>
            )}
          </label>
          {stage === "pooled" && (
            <label className="text-sm">
              <span className="font-medium">{l("泡池原因（可选）", "Reason for hold (optional)")}</span>
              <textarea
                value={pauseReason}
                onChange={(event) => setPauseReason(limitCodePoints(event.target.value, 1_000))}
                rows={2}
                placeholder={l("例如：等待 HC、招聘暂缓", "For example: headcount pending or hiring paused")}
                className="input mt-1 w-full resize-y"
              />
            </label>
          )}
          {!stageEndsApplication(stage) && (
            <details className="rounded-xl border border-line bg-panel-2/50 px-3 py-2">
              <summary className="cursor-pointer text-sm font-medium text-ink-2">
                {l("添加下一步安排（可选）", "Add next action (optional)")}
              </summary>
              <div className="mt-3 grid gap-3 sm:grid-cols-2">
                <label className="text-sm">
                  <span className="font-medium">{l("完成后阶段（可不变）", "Stage after completion (may stay the same)")}</span>
                  <select value={nextStage} onChange={(event) => {
                    setNextStage(event.target.value as ApplicationStage);
                    setNextStageManuallyEdited(true);
                  }} className="input mt-1 w-full">
                    {columns.map(([key, label]) => <option key={key} value={key}>{label}</option>)}
                  </select>
                </label>
                <label className="text-sm">
                  <span className="font-medium">{l("下一步", "Next step")}</span>
                  <input value={nextStep} onChange={(event) => setNextStep(limitCodePoints(event.target.value, 300))} placeholder={l("例如：一面 / 在线测评", "For example: first interview / online assessment")} className="input mt-1 w-full" />
                </label>
                <label className="text-sm">
                  <span className="font-medium">{l("日期", "Date")}</span>
                  <input type="date" value={nextDate} onChange={(event) => {
                    setNextDate(event.target.value);
                    if (!event.target.value) setNextTime("");
                  }} className="input mt-1 w-full" />
                </label>
                <label className="text-sm">
                  <span className="font-medium">{l("时间", "Time")}</span>
                  <input type="time" value={nextTime} disabled={!nextDate} onChange={(event) => setNextTime(event.target.value)} className="input mt-1 w-full" />
                </label>
                <label className="text-sm sm:col-span-2">
                  <span className="font-medium">{l("说明", "Note")}</span>
                  <textarea value={nextNote} onChange={(event) => setNextNote(limitCodePoints(event.target.value, 2_000))} rows={3} className="input mt-1 w-full resize-y" />
                </label>
              </div>
            </details>
          )}
          {stageEndsApplication(stage) && (nextStep.trim() || nextDate || nextTime || nextNote.trim()) && (
            <p role="status" className="rounded-lg bg-info-soft px-3 py-2 text-xs text-info">
              {l("这项安排会保留在当前表单中，但结束阶段不会保存下一步。切换到其他阶段后可继续编辑。", "This action remains in the form, but terminal stages do not save a next action. Switch to another stage to continue editing it.")}
            </p>
          )}
          <label className="text-sm">
            <span className="font-medium">{l("岗位描述（JD）", "Job description")}</span>
            <textarea value={jdText} onChange={(event) => setJdText(limitCodePoints(event.target.value, 50_000))} rows={7} className="input mt-1 w-full resize-y" />
          </label>
          {error && <p role="alert" className="rounded-lg bg-bad-soft px-3 py-2 text-sm text-bad">{error}</p>}
          <div className="flex justify-end gap-2 pt-1">
            <button type="button" onClick={onCancel} disabled={saving} className="btn">{l("取消", "Cancel")}</button>
            <button type="submit" disabled={saving || !company.trim() || !position.trim()} className="btn-primary">
              {saving ? l("正在新增…", "Adding…") : l("新增岗位", "Add role")}
            </button>
          </div>
          </fieldset>
        </form>
      </div>
    </div>
  );
}
