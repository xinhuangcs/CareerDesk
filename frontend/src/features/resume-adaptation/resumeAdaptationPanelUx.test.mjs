import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";

const panelUrl = new URL("./ResumeAdaptationPanel.tsx", import.meta.url);

test("the adaptation report is rendered as text with distinct trust boundaries", async () => {
  const source = await readFile(panelUrl, "utf8");
  assert.doesNotMatch(source, /\bMarkdown\b|dangerouslySetInnerHTML|innerHTML/);
  assert.match(source, /whitespace-pre-wrap/);
  assert.match(source, /本次分析范围/);
  assert.match(source, /查看不确定项/);
  assert.match(source, /未结合公司调研/);
  assert.match(source, /基于压缩摘要，非全文逐段分析/);
});

test("full and gap reports stay mutually exclusive and rewrites use the fixed four-row layout", async () => {
  const source = await readFile(panelUrl, "utf8");
  assert.match(source, /response\.report\.mode === "full"[\s\S]*?<FullReport[\s\S]*?: <GapReport/);
  for (const label of [
    "原文（提取自简历）",
    "建议写法（简历原语言）",
    "为什么这样改",
    "需要你核实/补充的事实（如有）",
  ]) assert.match(source, new RegExp(label.replace(/[()]/g, "\\$&")));
  assert.match(source, /defaultExpandedSectionIds\(report\.section_reviews\)/);
  assert.match(source, /report\.section_reviews\.map/);
});

test("binding is a disclosed two-step flow and research stays behind the host callback", async () => {
  const source = await readFile(panelUrl, "utf8");
  const binding = source.match(/async function bindSelectedResume\(\)[\s\S]*?\n  }\n\n  async function triggerResearch/)?.[0] ?? "";
  assert.ok(binding);
  assert.match(binding, /bindApplicationResume/);
  assert.match(binding, /readCurrent\(true\)/);
  assert.doesNotMatch(binding, /runGeneration|generateResumeAdaptation/);
  assert.match(binding, /expected_edit_revision: editRevision/);
  assert.match(source, /确认使用此版本/);
  assert.match(source, /确认切换版本/);
  assert.match(source, /await onResearchAction\(action\)/);
  assert.doesNotMatch(source, /prep_status/);
  assert.match(source, /continueAfterResearchRef/);
});

test("confirmations are per-request and waiting accurately describes background completion", async () => {
  const source = await readFile(panelUrl, "utf8");
  assert.match(source, /本次报告不包含公司调研背景/);
  assert.match(source, /额外调用模型；超长文件可能分块多次。报告没有逐段点评/);
  assert.match(source, /acceptNoResearch: confirmation === "no_research"/);
  assert.match(source, /acceptSummarized: confirmation === "summarized"/);
  assert.match(source, /confirmation === "summarized" && pendingNoResearchIntent/);
  assert.match(source, /next\.state === "insufficient_model_capacity"/);
  assert.match(source, /确认使用压缩摘要且不含调研/);
  assert.match(source, /完整 JD 与简历可提取文本发送给/);
  assert.match(source, /简历可提取文本、完整 JD 和可用调研结论发送给/);
  assert.match(source, /调研成功且本页仍打开时，会把可提取的简历全文、完整 JD 和新调研结论发送给/);
  assert.match(source, /输入量会在调研内容准备好后计算/);
  assert.doesNotMatch(source, /重新生成会把可提取的简历全文、完整 JD 和可用调研结论发送给/);
  assert.match(source, /配置模型后生成调研/);
  assert.match(source, /formatAdaptationElapsed\(elapsedSeconds, locale\)/);
  assert.match(source, /后台仍会继续完成当前任务/);
  assert.match(source, /通常需要约 1 分钟/);
  assert.match(source, /最长等待 3 分钟/);
  assert.match(source, /l\("停止等待", "Stop waiting"\)/);
  assert.match(source, /generationControllerRef\.current\?\.abort\(\)/);
  assert.match(source, /response\?\.state !== "generation_running"/);
  assert.match(source, /read: \(\) => readCurrent\(true\)/);
  assert.match(source, /正在读取后台生成进度/);
  assert.match(source, /formatAdaptationDateTime\(response\.research\.fresh_until, locale\)/);
  assert.match(source, /formatAdaptationDateTime\(response\.envelope\.generated_time, locale\)/);
  assert.match(source, /l\("当前材料：", "Current material: "\)/);
  assert.match(source, /response\.state === "ok" && response\.report && response\.envelope/);
  assert.match(source, /coverage_quality === "complete"\) return en \? "Complete" : "已完成"/);
  assert.doesNotMatch(source, /调研：\{response\.research\.coverage_quality/);
  assert.match(source, /生成时间：\$\{formatAdaptationDateTime\(response\.envelope\.generated_time, locale\)\}/);
  assert.doesNotMatch(source, /有效缓存/);
  assert.match(source, /onStatusChange\?\.\(panelStatus\)/);
  assert.match(source, /researchFollowupActive, runGeneration\]\);/);
  assert.doesNotMatch(source, /researchFollowupActive, response\?\.state, runGeneration/);
  assert.doesNotMatch(source, /localStorage|sessionStorage/);
});

test("dialogs receive focus and preview requests are cancelled with the panel", async () => {
  const source = await readFile(panelUrl, "utf8");
  assert.match(source, /dialogRef\.current\?\.focus\(\)/);
  assert.match(source, /tabIndex=\{-1\}/);
  assert.match(source, /previewControllerRef\.current\?\.abort\(\)/);
});

test("the panel exposes the actual model input preview without a legacy report path", async () => {
  const source = await readFile(panelUrl, "utf8");
  assert.match(source, /getResumeAdaptationInputPreview/);
  assert.match(source, /以下是将发送给模型的压缩摘要，不是简历原文/);
  assert.doesNotMatch(source, /旧版匹配报告|legacy_match|LegacyResumeMatch|resume-match/);
});
