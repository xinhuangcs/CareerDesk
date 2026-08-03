import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const grillPageUrl = new URL("./GrillPage.tsx", import.meta.url);
const grillLabPageUrl = new URL("./GrillLabPage.tsx", import.meta.url);
const grillIntroUrl = new URL("./GrillExperimentIntro.tsx", import.meta.url);
const grillSettingsUrl = new URL("./GrillVisibilitySettingsSection.tsx", import.meta.url);
const appUrl = new URL("../../app/App.tsx", import.meta.url);

test("navigation separates the experimental lab from management", async () => {
  const source = await readFile(appUrl, "utf8");
  const primaryStart = source.indexOf("const NAV_PRIMARY");
  const primaryEnd = source.indexOf("const NAV_LAB", primaryStart);
  const primary = source.slice(primaryStart, primaryEnd);
  const labEnd = source.indexOf("const NAV_MANAGEMENT", primaryEnd);
  const lab = source.slice(primaryEnd, labEnd);
  const management = source.slice(labEnd, source.indexOf("const PAGE_META", labEnd));

  assert.ok(primary.indexOf("APP_ROUTE_PATHS.timeline") >= 0);
  assert.doesNotMatch(primary, /APP_ROUTE_PATHS\.grill|APP_ROUTE_PATHS\.questions/);
  assert.match(lab, /APP_ROUTE_PATHS\.grill/);
  assert.match(lab, /labelKey: "shell\.nav\.grill"/);
  assert.match(lab, /badgeKey: "shell\.experimental"/);
  assert.doesNotMatch(lab, /APP_ROUTE_PATHS\.questions/);
  assert.match(management, /labelKey: "shell\.nav\.library"/);
  assert.match(source, /titleKey: "shell\.page\.grill\.title"/);
  assert.match(source, /<span className="section-label">\{t\("shell\.nav\.lab"\)\}<\/span>/);
  assert.match(source, /<span className="section-label">\{t\("shell\.nav\.management"\)\}<\/span>/);
  assert.match(source, /border-transparent bg-panel-2 text-ink/);
  assert.doesNotMatch(source, /border-line-strong bg-panel text-ink shadow-\[var\(--shadow-card\)\]/);
  assert.doesNotMatch(source, /before:(?:absolute|inset-y|left|w-|rounded|bg-accent)/);
});

test("the lab gives questions full width while keeping practice comfortably narrow", async () => {
  const appSource = await readFile(appUrl, "utf8");
  const labSource = await readFile(grillLabPageUrl, "utf8");
  const grillSource = await readFile(grillPageUrl, "utf8");

  assert.doesNotMatch(appSource, /APP_ROUTE_PATHS\.grill\s*\? "max-w-3xl"/);
  assert.match(labSource, /id="grill-practice-panel"[\s\S]*className="w-full max-w-3xl"/);
  assert.match(labSource, /id="grill-questions-panel"[\s\S]*className="w-full"/);
  assert.match(grillSource, /mx-auto max-w-3xl/);
  assert.match(grillSource, /l\("生成题集", "Generate a question set"\)[\s\S]*role="group" aria-label=\{l\("选择练习方式", "Choose a practice type"\)\}/);
  assert.match(grillSource, /button-wrap min-w-0 cursor-pointer rounded-xl border p-4 text-left/);
  assert.match(grillSource, /border-t border-line pt-4/);
  assert.match(grillSource, /output_locale: currentOutputLocale\(\)/);
});

test("the lab exposes two URL-backed tabs and keeps practice mounted", async () => {
  const source = await readFile(grillLabPageUrl, "utf8");

  assert.match(source, /role="tablist" aria-label=\{l\("拷打室内容", "Interview Lab content"\)\}/);
  assert.match(source, /l\("开始练习", "Practice"\)/);
  assert.match(source, /l\("题库", "Question Bank"\)/);
  assert.match(source, /searchParams\.get\("view"\) === "questions"/);
  assert.match(source, /next\.set\("view", "questions"\)/);
  assert.match(source, /<GrillPage \/>/);
  assert.match(source, /questionsMounted && <QuestionsPage \/>/);
});

test("the experimental lab introduces each installed release once and offers an exact settings path", async () => {
  const [lab, intro, settings, app] = await Promise.all([
    readFile(grillLabPageUrl, "utf8"),
    readFile(grillIntroUrl, "utf8"),
    readFile(grillSettingsUrl, "utf8"),
    readFile(appUrl, "utf8"),
  ]);

  assert.match(lab, /claimGrillExperimentIntro\(\)/);
  assert.match(lab, /grillExperimentIntroWasSeen\(release_version\)/);
  assert.match(lab, /markGrillExperimentIntroSeen\(release_version\)/);
  assert.match(lab, /setShowExperimentIntro\(should_show\)/);
  assert.doesNotMatch(lab, /setShowExperimentIntro\(should_show && !previouslySeen\)/);
  assert.match(lab, /if \(!experimentIntroResolved\) return null/);
  assert.match(lab, /search: "\?section=experiments"/);
  assert.match(intro, /role="dialog"/);
  assert.match(intro, /aria-modal="true"/);
  assert.match(intro, /l\("开始体验", "Start exploring"\)/);
  assert.match(intro, /l\("暂时不需要，去设置", "Not now—open settings"\)/);
  assert.match(intro, /根据你的简历或者投递的岗位生成可能的面试题目/);
  assert.match(intro, /AI提供的反馈仅供参考/);
  assert.match(settings, /id="settings-experiments"/);
  assert.match(settings, /id="show-grill-navigation"/);
  assert.match(settings, /隐藏入口不会删除任何数据/);
  assert.match(app, /visibleLabNavigation\.length > 0/);
  assert.match(app, /subscribeToGrillVisibility\(setGrillNavigationVisible\)/);
});

test("capacity failures distinguish missing metadata from real context overflow", async () => {
  const source = await readFile(grillPageUrl, "utf8");

  assert.match(source, /model_capacity_required/);
  assert.match(source, /当前模型缺少可信容量信息/);
  assert.match(source, /当前模型上下文不足/);
});

test("grill narrow layouts constrain controls and unbroken content", async () => {
  const source = await readFile(grillPageUrl, "utf8");

  assert.match(source, /textarea className="input min-h-40 w-full"/);
  assert.equal((source.match(/button-wrap min-w-0 cursor-pointer rounded-xl/g) ?? []).length, 2);
  assert.match(source, /flex flex-col items-start gap-3 sm:flex-row sm:justify-between/);
  assert.match(source, /break-words font-medium/);
  assert.match(source, /grid gap-3 md:grid-cols-2/);
  assert.match(source, /flex flex-wrap items-center justify-between gap-3/);
  assert.match(source, /l\("可用题集", "Available question sets"\)[\s\S]*l\("每次题数", "Questions per session"\).*<input type="number"/);
  assert.match(source, /max=\{maximumAvailableQuestionCount \|\| undefined\}/);
  assert.match(source, /l\("岗位描述：", "Job description: "\)/);
  assert.match(source, /去简历库添加该岗位的简历/);
  assert.match(source, /点击“编辑”后补充岗位描述/);
  assert.match(source, /生成通常需要 2–5 分钟/);
  assert.match(source, /showVersionNotice && <div className="flex items-start gap-3 rounded-lg bg-panel-2/);
  assert.match(source, /onClick=\{\(\) => void retryGeneration\(item\)\}/);
  assert.match(source, /aria-busy=\{retryingSetId === item\.id\}/);
  assert.match(source, /refresh: refreshIntent \|\| hasCurrentSetForSelection/);
  assert.match(source, /refreshIntent \|\| hasCurrentSetForSelection \? l\("生成新题集", "Generate new set"\) : l\("生成题集", "Generate set"\)/);
  assert.doesNotMatch(source, /onClick=\{\(\) => prepareRegeneration\(item\)\}>重试<\/button>/);
});

test("finished practice renders readable feedback and guide content instead of JSON", async () => {
  const source = await readFile(grillPageUrl, "utf8");

  assert.match(source, /presentReplayReview\(feedback, answerGuide\)/);
  assert.match(source, /l\("做得好的地方", "What worked well"\)/);
  assert.match(source, /l\("可以改进", "What to improve"\)/);
  assert.match(source, /l\("下一步建议", "Recommended next step"\)/);
  assert.match(source, /l\("回答指南", "Answer guide"\)/);
  assert.doesNotMatch(source, /JSON\.stringify\(\{ feedback: item\.feedback, answer_guide: item\.answer_guide \}/);
});

test("grill notices stay dismissed after their close buttons are used", async () => {
  const source = await readFile(grillPageUrl, "utf8");

  assert.match(source, /window\.localStorage\.getItem\(key\) === "1"/);
  assert.match(source, /window\.localStorage\.setItem\(key, "1"\)/);
  assert.match(source, /aria-label=\{l\("关闭模型发送提示", "Dismiss model-data notice"\)\}/);
  assert.match(source, /aria-label=\{l\("关闭题集版本提示", "Dismiss version notice"\)\}/);
});
