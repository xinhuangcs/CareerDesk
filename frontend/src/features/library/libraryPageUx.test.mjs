import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { test } from "node:test";
import { parseLibraryDeepLink, safeLibraryReturnPath } from "./libraryDeepLink.ts";

const pageUrl = new URL("./LibraryPage.tsx", import.meta.url);

test("resume work stays visible while completed jobs collapse into recent history", async () => {
  const source = await readFile(pageUrl, "utf8");

  assert.match(source, /activeOrFailedResumeJobs\.slice\(0, 5\)/);
  assert.match(source, /<details className="mb-4[^"]*"/);
  assert.match(source, /l\("最近完成", "Recently completed"\).*completedResumeJobs\[0\]\.name/);
});

test("terminal resume jobs and page errors expose explicit dismiss controls", async () => {
  const source = await readFile(pageUrl, "utf8");

  assert.match(source, /job\.state !== "processing"/);
  assert.match(source, /\/api\/resumes\/jobs\/\$\{job\.job_id\}/);
  assert.match(source, /关闭简历任务提示/);
  assert.match(source, /aria-label=\{l\("关闭错误提示", "Dismiss error"\)\}/);
  assert.match(source, /aria-label=\{l\("关闭加载错误提示", "Dismiss loading error"\)\}/);
});

test("dismissed resume job tombstones reject every stale snapshot for the page lifetime", async () => {
  const source = await readFile(pageUrl, "utf8");

  assert.match(source, /dismissedResumeJobIdsRef = useRef<Set<string>>\(new Set\(\)\)/);
  assert.match(source, /serverItems\.filter\(\(job\) => !dismissedIds\.has\(job\.job_id\)\)/);
  assert.match(source, /dismissedResumeJobIdsRef\.current\.add\(job\.job_id\)/);
  assert.doesNotMatch(source, /dismissedResumeJobIdsRef\.current\s*=/);
  assert.match(source, /await refresh\(\)/);
});

test("resume cards use the lightweight list contract without full text", async () => {
  const source = await readFile(pageUrl, "utf8");

  assert.match(source, /formatNumber\(r\.character_count, locale\)/);
  assert.match(source, /\/api\/resumes\/\$\{resume\.id\}\/text/);
  assert.doesNotMatch(source, /r\.content_text|r\.lines/);
});

test("resume cards expose only general or position-specific gray labels", async () => {
  const source = await readFile(pageUrl, "utf8");

  assert.match(source, /l\("通用版", "General"\)/);
  assert.match(source, /l\(`岗位专属-\$\{r\.application_company\}-\$\{r\.application_position\}`/);
  assert.match(source, /tag shrink-0 bg-panel-2 text-ink-2/);
  assert.doesNotMatch(source, /FAMILY_LABELS|r\.family|简历岗位族|resume-families|语义标注已完成/);
});

test("resume cards load extracted text on demand before the update action", async () => {
  const source = await readFile(pageUrl, "utf8");

  assert.match(source, /viewResumeText\(r\)[\s\S]*?l\("查看", "View"\)[\s\S]*?pickResumeUpdate\(r\.id\)/);
  assert.match(source, /role="dialog"/);
  assert.match(source, /viewingResume\.content_text/);
  assert.match(source, /aria-label=\{l\("关闭简历文字", "Close résumé text"\)\}/);
  assert.match(source, /识别结果可能与原文件存在细微差别，请核对后按需修改/);
  assert.match(source, /aria-label=\{l\("编辑简历校正版文字", "Edit corrected résumé text"\)\}/);
  assert.match(source, /expected_content_hash: viewingResume\.content_hash/);
  assert.match(source, /savingResumeText \? l\("保存中…", "Saving…"\) : l\("保存修改", "Save changes"\)/);
});

test("resume deep links keep only a safe Timeline return", () => {
  assert.deepEqual(parseLibraryDeepLink(
    "?resumeId=17&returnTo=%2Ftimeline%3Fapplication%3D9%26tab%3Dadaptation",
  ), {
    resumeId: 17,
    returnTo: "/timeline?application=9&tab=adaptation",
  });
  assert.deepEqual(parseLibraryDeepLink("?resumeId=-1"), {
    resumeId: null,
    returnTo: null,
  });
  assert.equal(safeLibraryReturnPath("https://example.com/timeline"), null);
  assert.equal(safeLibraryReturnPath("//example.com/timeline"), null);
  assert.equal(safeLibraryReturnPath("/settings"), null);
  assert.equal(safeLibraryReturnPath("/timeline#unexpected"), null);
});

test("the Library performs one rendered-section focus and exposes the safe return", async () => {
  const source = await readFile(pageUrl, "utf8");
  assert.match(source, /parseLibraryDeepLink\(location\.search\)/);
  assert.match(source, /focusedResumeDeepLinkRef/);
  assert.match(source, /requestAnimationFrame/);
  assert.match(source, /scrollIntoView\(\{ behavior: "smooth", block: "start" \}\)/);
  assert.match(source, /focus\(\{ preventScroll: true \}\)/);
  assert.match(source, /ref=\{resumeUploadCardRef\}/);
  assert.match(source, /id="library-resume-upload"/);
  assert.match(source, /data-resume-id=\{r\.id\}/);
  assert.match(source, /l\("返回岗位", "Back to role"\)/);
});
