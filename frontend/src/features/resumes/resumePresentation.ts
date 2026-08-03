import type { UiLocale } from "../../i18n/i18n";
import type { ResumeJob } from "./resumeContract";

export function resumeJobMessage(job: ResumeJob, locale: UiLocale): string {
  const en = locale === "en";
  if (job.state === "processing") {
    return en
      ? "The local service will keep working; navigating away or refreshing will not cancel the task."
      : "任务由本地服务继续执行，切换页面或刷新不会取消。";
  }
  if (job.state === "completed") {
    return en ? "Résumé parsed and saved." : "简历已解析并保存。";
  }

  const message = job.message ?? "";
  if (message.includes("超过 60 秒")) {
    return en
      ? "Reading the document took more than 60 seconds and was stopped. If it is a scanned PDF, run OCR and retry."
      : "文档读取超过 60 秒，已停止等待。若是扫描版 PDF，请先 OCR 后重试。";
  }
  if (message.includes("任务已中断")) {
    return en
      ? "The task was interrupted, possibly because the app exited. Upload the résumé again."
      : "任务已中断（应用可能曾退出），请重新上传。";
  }
  if (message.includes("不能为空")) {
    return en
      ? "No readable résumé text was found. If this is a scanned PDF, run OCR and retry."
      : "未找到可读取的简历文字。若是扫描版 PDF，请先 OCR 后重试。";
  }
  if (message.includes("200,000")) {
    return en
      ? "The résumé exceeds the 200,000-character limit. Shorten it and retry."
      : "简历超过 200,000 字符上限，请精简后重试。";
  }
  if (message.includes("段落")) {
    return en
      ? "The résumé contains too many fragmented lines. Remove repeated or character-by-character line breaks and retry."
      : "简历包含过多零碎段落，请移除重复内容或逐字换行后重试。";
  }
  return en
    ? "The résumé could not be processed. Check that the file is readable and retry."
    : "简历处理失败，请确认文件可读取后重试。";
}

export function resumeMutationErrorMessage(
  message: string | undefined,
  locale: UiLocale,
  action: "upload" | "update",
): string {
  const en = locale === "en";
  const raw = message ?? "";
  if (raw.includes("同名") || raw.toLowerCase().includes("same name")) {
    return en
      ? "A résumé with this version name already exists. Use another name or update the existing version."
      : "同名版本已存在，请改用其他版本名或更新现有版本。";
  }
  if (raw.includes("不支持的简历格式") || raw.toLowerCase().includes("unsupported")) {
    return en
      ? "This résumé format is not supported. Use PDF, DOCX, Markdown, or plain text."
      : "不支持这种简历格式，请使用 PDF、DOCX、Markdown 或纯文本。";
  }
  if (raw.includes("已归档")) {
    return en
      ? "This résumé is archived and cannot be updated. Create a new version instead."
      : "该简历已归档，无法更新；请新建一个版本。";
  }
  if (raw.includes("找不到简历")) {
    return en ? "This résumé no longer exists." : "这份简历已不存在。";
  }
  if (raw.includes("正在处理") || raw.includes("任务正在运行")) {
    return en
      ? "This résumé is already being processed. Wait for the current task to finish."
      : "这份简历已有处理任务，请等待当前任务完成。";
  }
  if (raw.includes("已更新") || raw.includes("状态已变化")) {
    return en
      ? "This résumé changed elsewhere. Refresh and retry."
      : "这份简历已在其他位置更新，请刷新后重试。";
  }
  if (action === "update") return en ? "Could not update the résumé." : "简历更新失败。";
  return en ? "Could not upload the résumé." : "简历上传失败。";
}
