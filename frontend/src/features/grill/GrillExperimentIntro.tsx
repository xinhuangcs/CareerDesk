import { useEffect, useRef } from "react";
import { useLocalizer } from "../../i18n/useLocalizer";

type GrillExperimentIntroProps = {
  onContinue: () => void;
  onOpenSettings: () => void;
};

export function GrillExperimentIntro({
  onContinue,
  onOpenSettings,
}: GrillExperimentIntroProps) {
  const l = useLocalizer();
  const continueRef = useRef<HTMLButtonElement | null>(null);
  const capabilities = [
    ["01", l("岗位化题集", "Role-specific questions"), l("围绕你的简历与目标岗位组织练习", "Practice built around your résumé and target role")],
    ["02", l("连续追问", "Adaptive follow-ups"), l("在回答之后继续探查思路与证据", "Follow-up questions that probe your reasoning and evidence")],
    ["03", l("针对性反馈", "Focused feedback"), l("把薄弱点整理成下一轮练习方向", "Turn weak spots into clear goals for your next session")],
  ] as const;

  useEffect(() => {
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    continueRef.current?.focus();
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onContinue();
    };
    window.addEventListener("keydown", onKeyDown);
    return () => {
      document.body.style.overflow = previousOverflow;
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [onContinue]);

  return (
    <div className="fixed inset-0 z-[80] flex items-center justify-center bg-ink/35 p-4 backdrop-blur-[6px]">
      <section
        role="dialog"
        aria-modal="true"
        aria-labelledby="grill-experiment-title"
        aria-describedby="grill-experiment-description"
        className="relative max-h-[min(92vh,760px)] w-full max-w-[560px] overflow-y-auto rounded-[28px] border border-line bg-panel shadow-[var(--shadow-pop)]"
      >
        <div className="relative overflow-hidden rounded-t-[27px] bg-ink px-6 pb-7 pt-6 text-accent-ink sm:px-8 sm:pb-8 sm:pt-7">
          <div className="pointer-events-none absolute -right-16 -top-20 h-52 w-52 rounded-full border border-accent-ink/10" />
          <div className="pointer-events-none absolute -right-6 -top-8 h-32 w-32 rounded-full bg-ember/25 blur-2xl" />
          <div className="relative flex items-start justify-between gap-4">
            <div>
              <span className="inline-flex items-center gap-1.5 rounded-full border border-accent-ink/15 bg-accent-ink/10 px-2.5 py-1 text-[11px] font-medium tracking-wide">
                <span className="h-1.5 w-1.5 rounded-full bg-ember" />
                EXPERIMENTAL
              </span>
              <p className="mt-6 text-xs text-accent-ink/55">CAREERDESK LAB · 01</p>
            </div>
            <div className="flex h-12 w-12 items-center justify-center rounded-2xl border border-accent-ink/15 bg-accent-ink/10 text-ember">
              <svg viewBox="0 0 24 24" className="h-6 w-6" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M12.6 2.8c.5 2.9-.7 4.5-2.1 6C9 10.4 7.4 12 7.4 14.7a5 5 0 0 0 10 .4c.1-2.5-1.2-4.6-2.5-6.2-.3 1.4-1.2 2.4-2.2 2.9-.3-2.8.3-5.9-.1-9Z" />
              </svg>
            </div>
          </div>
          <h2 id="grill-experiment-title" className="relative mt-4 max-w-md text-[28px] font-semibold leading-tight tracking-[-0.025em] sm:text-[32px]">
            {l("欢迎来到拷打室", "Welcome to Interview Lab")}
          </h2>
          <p id="grill-experiment-description" className="relative mt-3 max-w-md text-sm leading-6 text-accent-ink/70">
            {l("这是一个仍在打磨的模拟面试训练场。它会根据你的简历或者投递的岗位生成可能的面试题目、答题时可以模拟面试环境继续追问，并在答题后给出提升建议。", "This experimental interview simulator creates likely questions from your résumé or target role, asks realistic follow-ups, and gives practical feedback after each answer.")}
          </p>
        </div>

        <div className="px-6 pb-6 pt-5 sm:px-8 sm:pb-7">
          <div className="divide-y divide-line-2">
            {capabilities.map(([index, title, description]) => (
              <div key={index} className="grid grid-cols-[32px_minmax(0,1fr)] gap-3 py-3 first:pt-0">
                <span className="pt-0.5 text-xs font-medium tabular-nums text-ember">{index}</span>
                <div>
                  <p className="text-sm font-medium text-ink">{title}</p>
                  <p className="mt-0.5 text-xs leading-relaxed text-ink-3">{description}</p>
                </div>
              </div>
            ))}
          </div>
          <p className="mt-1 rounded-xl bg-panel-2 px-3.5 py-3 text-xs leading-relaxed text-ink-2">
            {l("实验功能可能继续调整，AI提供的反馈仅供参考，不替代你的专业判断。题集和练习记录仍只保存在本机。", "This experimental feature may change. AI feedback is a learning aid, not a substitute for your judgment. Question sets and practice history remain on this device.")}
          </p>
          <div className="mt-5 flex flex-col gap-2.5 sm:flex-row sm:items-center">
            <button ref={continueRef} type="button" className="btn-primary sm:min-w-32" onClick={onContinue}>
              {l("开始体验", "Start exploring")}
            </button>
            <button type="button" className="btn sm:ml-auto" onClick={onOpenSettings}>
              {l("暂时不需要，去设置", "Not now—open settings")}
            </button>
          </div>
        </div>
      </section>
    </div>
  );
}
