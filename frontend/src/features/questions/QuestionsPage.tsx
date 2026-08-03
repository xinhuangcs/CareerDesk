import { useEffect, useMemo, useState } from "react";
import { getJson, putJson } from "../../shared/api/transport";
import { useLocalizer } from "../../i18n/useLocalizer";

type PracticeType = "basic" | "custom";

type Question = {
  id: number;
  text: string;
  source: "generated";
  quality_flag: "good" | "bad" | null;
  category: string;
  channel: "interview" | "written";
  primary_competency: string;
  secondary_tags: string[];
  answer_guide: Record<string, unknown> | null;
  answer_verified: boolean;
  question_set_id: number;
  edition: PracticeType;
  context_label: string;
};

function answerGuideText(guide: Record<string, unknown> | null): string | null {
  const text = guide?.text;
  return typeof text === "string" && text.trim() ? text : null;
}

type Response = { items: Question[] };
type CompetencyProgress = {
  aggregate: { competency: string; practice_count: number; last_asked_time: string | null; performance_points: number; scope_count: number }[];
  scopes: { scope_kind: "global" | "resume" | "application"; scope_ref: string; context_label: string; competency: string; box: number; practice_count: number; last_verdict: string | null; due_date: string | null }[];
};

export function QuestionsPage() {
  const l = useLocalizer();
  const category: Record<string, string> = useMemo(() => ({
    hr_motivation: l("动机与匹配", "Motivation & fit"), resume_deep_dive: l("简历深挖", "Résumé deep dive"),
    behavioral_situational: l("行为与情境", "Behavioral & situational"), professional_domain: l("专业领域", "Professional expertise"),
    business_company: l("业务与公司", "Business & company"), case_work_sample: l("案例与作业", "Case & work sample"),
  }), [l]);
  const [items, setItems] = useState<Question[]>([]);
  const [practiceType, setPracticeType] = useState<PracticeType>("basic");
  const [progress, setProgress] = useState<CompetencyProgress>({ aggregate: [], scopes: [] });
  const [query, setQuery] = useState("");
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const visible = useMemo(() => items.filter((item) => !normalizedQuery || [
    item.text, category[item.category] ?? item.category, item.primary_competency,
    item.context_label, ...item.secondary_tags,
  ].join(" ").toLocaleLowerCase().includes(normalizedQuery)), [category, items, normalizedQuery]);

  async function load() {
    setLoading(true); setError("");
    try {
      const [result, competency] = await Promise.all([
        getJson<Response>(`/api/questions?edition=${practiceType}`),
        getJson<CompetencyProgress>("/api/questions/competency-progress"),
      ]);
      setItems(result.items);
      setProgress(competency);
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : l("题库加载失败", "Could not load the question bank"));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, [practiceType]);

  async function quality(id: number, flag: "good" | "bad" | null) {
    try {
      await putJson(`/api/questions/${id}/quality`, { flag });
      setItems((current) => current.map((item) => item.id === id ? { ...item, quality_flag: flag } : item));
    } catch (reason) { setError(reason instanceof Error ? reason.message : l("题目标记失败", "Could not update the question rating")); }
  }

  async function verify(id: number, verified: boolean) {
    try {
      await putJson(`/api/questions/${id}/answer-guide-verification`, { verified });
      setItems((current) => current.map((item) => item.id === id ? { ...item, answer_verified: verified } : item));
    } catch (reason) { setError(reason instanceof Error ? reason.message : l("回答指南确认失败", "Could not update answer-guide verification")); }
  }

  return <section className="space-y-5">
    <header><h2 className="text-xl font-semibold">{l("题库", "Question Bank")}</h2></header>
    {error && <p className="rounded-xl bg-bad-soft p-3 text-sm text-bad">{error}</p>}
    {progress.aggregate.length > 0 && <details className="card p-4"><summary className="cursor-pointer font-medium">{l("练习重点进展", "Competency progress")} · {progress.aggregate.length}</summary><div className="mt-4 grid gap-3 md:grid-cols-2">{progress.aggregate.map((item) => <div className="min-w-0 rounded-xl bg-panel-2 p-3" key={item.competency}><div className="flex flex-wrap justify-between gap-2"><span className="min-w-0 break-words font-medium">{item.competency}</span><span className="text-sm text-ink-3">{l(`已练习 ${item.practice_count} 次`, `Practiced ${item.practice_count} ${item.practice_count === 1 ? "time" : "times"}`)}</span></div><details className="mt-2 text-sm"><summary className="cursor-pointer text-ink-3">{l("查看不同练习场景", "View practice contexts")}</summary><ul className="mt-2 space-y-1 break-words">{progress.scopes.filter((scope) => scope.competency === item.competency).map((scope) => <li key={`${scope.scope_kind}:${scope.scope_ref}`}>{scope.context_label} · {l("记忆阶段", "memory stage")} {scope.box} · {scope.practice_count} {l("次", scope.practice_count === 1 ? "time" : "times")}</li>)}</ul></details></div>)}</div></details>}
    <div className="card space-y-4 p-4">
      <div className="grid grid-cols-2 gap-2 rounded-xl bg-panel-2 p-1" role="group" aria-label={l("按练习方式筛选", "Filter by practice type")}>
        {([['basic', l('通用练习', 'General practice')], ['custom', l('岗位定制', 'Role-specific')]] as const).map(([value, label]) => <button aria-pressed={practiceType === value} className={`button-wrap min-w-0 cursor-pointer rounded-lg px-2 py-2 text-sm font-medium leading-tight transition sm:px-4 ${practiceType === value ? "bg-panel text-ink shadow-sm" : "text-ink-3 hover:text-ink"}`} key={value} onClick={() => setPracticeType(value)}>{label}</button>)}
      </div>
      <label className="block"><span className="mb-2 block text-sm font-medium">{l("搜索题库", "Search questions")}</span><div className="flex gap-2"><input className="input min-w-0 flex-1" aria-label={l("搜索题目关键词", "Search question keywords")} value={query} onChange={(event) => setQuery(event.target.value)} placeholder={l("输入题目关键词", "Enter keywords")} />{query && <button className="btn-secondary shrink-0" onClick={() => setQuery("")}>{l("清空", "Clear")}</button>}</div></label>
      <p className="text-xs text-ink-3">{loading ? l("正在加载…", "Loading…") : l(`找到 ${visible.length} 道${practiceType === "basic" ? "通用练习" : "岗位定制"}题`, `${visible.length} ${practiceType === "basic" ? "general" : "role-specific"} ${visible.length === 1 ? "question" : "questions"}`)}</p>
    </div>
    {!loading && visible.length === 0 && <div className="card p-8 text-center"><p className="font-medium">{l("没有匹配的题目", "No matching questions")}</p><p className="mt-1 text-sm text-ink-3">{query ? l("换一个关键词，或清空搜索后重试。", "Try another keyword or clear the search.") : l("请先在“开始练习”中生成对应题集。", "Generate a question set from Practice first.")}</p></div>}
    <div className="space-y-3">{visible.map((item) => {
      const guideText = answerGuideText(item.answer_guide);
      return <article className="card p-5" key={item.id}><div className="flex flex-wrap items-center gap-2 text-xs text-ink-3"><span className="badge">{item.edition === "custom" ? l("岗位定制", "Role-specific") : l("通用练习", "General practice")}</span><span>{category[item.category] ?? item.category}</span><span>·</span><span>{item.channel === "written" ? l("笔试", "Written") : l("面试", "Interview")}</span>{item.answer_verified && <span className="text-ok">{l("回答指南已确认", "Answer guide verified")}</span>}</div><p className="mt-3 font-medium">{item.text}</p><div className="mt-3 rounded-lg bg-panel-2 p-3 text-sm"><p><span className="text-ink-3">{l("题集：", "Set: ")}</span>{item.context_label}</p><p className="mt-1"><span className="text-ink-3">{l("考察重点：", "Focus: ")}</span>{item.primary_competency}</p>{item.secondary_tags.length > 0 && <p className="mt-1 text-ink-3">{l("标签：", "Tags: ")}{item.secondary_tags.join(" · ")}</p>}</div><div className="mt-3 flex flex-wrap gap-2"><button className="btn-ghost text-sm" onClick={() => quality(item.id, item.quality_flag === "good" ? null : "good")}>{item.quality_flag === "good" ? l("取消好题", "Remove strong rating") : l("标记好题", "Mark as strong")}</button><button className="btn-ghost text-sm" onClick={() => quality(item.id, item.quality_flag === "bad" ? null : "bad")}>{item.quality_flag === "bad" ? l("取消需改进", "Remove needs-work rating") : l("标记需改进", "Needs improvement")}</button>{guideText && <button className="btn-ghost text-sm" onClick={() => verify(item.id, !item.answer_verified)}>{item.answer_verified ? l("撤销指南确认", "Undo verification") : l("确认回答指南", "Verify answer guide")}</button>}</div>{guideText && <details className="mt-3"><summary className="cursor-pointer text-sm">{l("回答指南（非唯一标准答案）", "Answer guide (not the only valid answer)")}</summary><p className="mt-2 whitespace-pre-wrap text-sm text-ink-3">{guideText}</p></details>}</article>;
    })}</div>
  </section>;
}
