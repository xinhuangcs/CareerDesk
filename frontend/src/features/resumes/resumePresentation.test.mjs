import assert from "node:assert/strict";
import { test } from "node:test";

import { resumeJobMessage, resumeMutationErrorMessage } from "./resumePresentation.ts";

function job(overrides = {}) {
  return {
    job_id: "00000000-0000-4000-8000-000000000001",
    operation: "create",
    target_resume_id: null,
    name: "Resume",
    state: "failed",
    stage: "failed",
    message: "简历处理失败，请确认文件可读取后重试。",
    resume_id: null,
    created_time: "2026-07-21T00:00:00+00:00",
    updated_time: "2026-07-21T00:00:00+00:00",
    ...overrides,
  };
}

test("resume job messages use UI locale instead of persisted backend copy", () => {
  assert.equal(
    resumeJobMessage(job({ message: "文档读取超过 60 秒，已停止等待。若是扫描版 PDF，请先 OCR 后重试。" }), "en"),
    "Reading the document took more than 60 seconds and was stopped. If it is a scanned PDF, run OCR and retry.",
  );
  assert.equal(
    resumeJobMessage(job({ message: "简历文本不能超过 200,000 个字符" }), "en"),
    "The résumé exceeds the 200,000-character limit. Shorten it and retry.",
  );
  assert.equal(
    resumeJobMessage(job({ message: "untrusted backend diagnostic" }), "en"),
    "The résumé could not be processed. Check that the file is readable and retry.",
  );
  assert.equal(
    resumeJobMessage(job({ state: "completed", stage: "completed", resume_id: 4 }), "en"),
    "Résumé parsed and saved.",
  );
});

test("resume mutation errors expose only localized known outcomes", () => {
  assert.equal(
    resumeMutationErrorMessage("同名已存在，请改版本名或使用更新", "en", "upload"),
    "A résumé with this version name already exists. Use another name or update the existing version.",
  );
  assert.equal(
    resumeMutationErrorMessage("opaque internal detail", "en", "update"),
    "Could not update the résumé.",
  );
});
