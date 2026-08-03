import { useEffect, useRef, useState, type DragEvent, type FormEvent, type KeyboardEvent } from "react";

import { desktopApi } from "../../shared/native/desktopBridge";
import { uploadWorkbookIntake } from "../intake-operations/intakeOperationApi";
import { useLocale } from "../../i18n/localePreference";
import { useLocalizer } from "../../i18n/useLocalizer";

const ACCEPTED_SUFFIXES = [".xlsx", ".xls", ".csv", ".tsv"];

function supportedFile(file: File): boolean {
  const name = file.name.toLowerCase();
  return ACCEPTED_SUFFIXES.some((suffix) => name.endsWith(suffix));
}

export function TimelineWorkbookImportDialog({
  onCancel,
  onPrepared,
}: {
  onCancel: () => void;
  onPrepared: (operationId: string, skippedRows: number) => void;
}) {
  const l = useLocalizer();
  const { locale } = useLocale();
  const [file, setFile] = useState<File | null>(null);
  const [dragging, setDragging] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [downloading, setDownloading] = useState(false);
  const [downloadedPath, setDownloadedPath] = useState("");
  const [error, setError] = useState("");
  const dialogRef = useRef<HTMLDivElement | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);

  useEffect(() => {
    const previousFocus = document.activeElement instanceof HTMLElement ? document.activeElement : null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    dialogRef.current?.focus();
    return () => {
      document.body.style.overflow = previousOverflow;
      previousFocus?.focus();
    };
  }, []);

  function choose(nextFile: File | null) {
    if (!nextFile) return;
    if (!supportedFile(nextFile)) {
      setFile(null);
      setError(l("请选择 xlsx、xls、csv 或 tsv 表格。", "Choose an xlsx, xls, csv, or tsv workbook."));
      return;
    }
    if (nextFile.size > 10 * 1024 * 1024) {
      setFile(null);
      setError(l("表格不能超过 10 MB。", "The workbook cannot exceed 10 MB."));
      return;
    }
    setFile(nextFile);
    setError("");
  }

  function drop(event: DragEvent<HTMLDivElement>) {
    event.preventDefault();
    setDragging(false);
    choose(event.dataTransfer.files.item(0));
  }

  function handleKeyDown(event: KeyboardEvent<HTMLDivElement>) {
    if (event.key === "Escape" && !uploading) {
      event.preventDefault();
      onCancel();
      return;
    }
    if (event.key !== "Tab") return;
    const focusable = Array.from(dialogRef.current?.querySelectorAll<HTMLElement>(
      "a[href], button:not([disabled]), input:not([disabled])",
    ) ?? []);
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (!first || !last) return;
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
    if (!file || uploading) return;
    setUploading(true);
    setError("");
    try {
      const result = await uploadWorkbookIntake(file);
      if (result.status === "preview" && result.operation_id) {
        onPrepared(result.operation_id, result.skipped_rows);
      } else if (result.status === "unrecognized") {
        setError(l("没有识别到“公司名称 + 岗位名称”列。可按示例整理，或把原表交给求职助手理解后导入。", "Could not find Company + Role Title columns. Match the example or ask Career Assistant to interpret and import the original workbook."));
      } else if (result.status === "empty") {
        setError(l("没有找到可安全导入的岗位。缺少公司或岗位名称的行不会写入。", "No roles could be imported safely. Rows without a company or role title are never saved."));
      } else {
        setError(l("这次预览已被更新的导入取代，请重新上传一次。", "A newer import replaced this preview. Upload the workbook again."));
      }
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : l("表格读取失败，请稍后重试。", "Could not read the workbook. Try again later."));
    } finally {
      setUploading(false);
    }
  }

  async function downloadTemplate() {
    if (downloading) return;
    setDownloading(true);
    setDownloadedPath("");
    setError("");
    try {
      const api = desktopApi();
      if (api?.download_job_import_template) {
        setDownloadedPath(await api.download_job_import_template(locale));
        return;
      }
      const anchor = document.createElement("a");
      anchor.href = locale === "en" ? "/careerdesk-job-import-example-en.xlsx" : "/careerdesk-job-import-example-zh-CN.xlsx";
      anchor.download = locale === "en" ? "CareerDesk-job-import-template.xlsx" : "CareerDesk-岗位导入模板.xlsx";
      document.body.append(anchor);
      anchor.click();
      anchor.remove();
      setDownloadedPath(l("浏览器默认下载目录", "Browser downloads folder"));
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : l("表格模板下载失败，请重试。", "Could not download the workbook template. Retry."));
    } finally {
      setDownloading(false);
    }
  }

  async function openDownloadedTemplate() {
    setError("");
    try {
      const api = desktopApi();
      if (!api?.open_job_import_template || downloadedPath === l("浏览器默认下载目录", "Browser downloads folder")) {
        setError(l("请在浏览器下载列表中打开模板。", "Open the template from your browser's downloads list."));
        return;
      }
      await api.open_job_import_template(downloadedPath);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : l("表格模板无法打开。", "Could not open the workbook template."));
    }
  }

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center p-4" role="presentation">
      <button type="button" tabIndex={-1} aria-label={l("关闭导入岗位", "Close role import")} onClick={() => { if (!uploading) onCancel(); }} className="absolute inset-0 cursor-default bg-black/40" />
      <div
        ref={dialogRef}
        tabIndex={-1}
        role="dialog"
        aria-modal="true"
        aria-labelledby="workbook-import-title"
        onKeyDown={handleKeyDown}
        className="relative z-10 w-full max-w-2xl rounded-2xl border border-line bg-panel p-5 outline-none"
        style={{ boxShadow: "var(--shadow-pop)" }}
      >
        <div className="flex items-start justify-between gap-4">
          <div>
            <h2 id="workbook-import-title" className="text-base font-semibold">{l("从表格导入岗位", "Import roles from a workbook")}</h2>
            <p className="mt-1 text-xs leading-5 text-ink-3">
              <span className="block">{l("每次最多 200 条，先预览、后写入。", "Up to 200 rows at a time. Preview first, then save.")}</span>
              <span className="block">{l("如果你不想使用表格模板，也可配置好大模型后通过求职助手进行智能化分析与导入。", "If you prefer not to use the workbook template, configure a model and ask Career Assistant to analyse and import your workbook.")}</span>
            </p>
          </div>
          <button
            type="button"
            onClick={onCancel}
            disabled={uploading}
            className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg text-lg leading-none text-ink-3 transition-colors hover:bg-panel-2 hover:text-ink disabled:cursor-not-allowed disabled:opacity-50"
            aria-label={l("关闭导入岗位", "Close role import")}
          >
            ×
          </button>
        </div>

        <form onSubmit={(event) => void submit(event)} className="mt-4">
          <div
            onDragEnter={(event) => { event.preventDefault(); setDragging(true); }}
            onDragOver={(event) => event.preventDefault()}
            onDragLeave={(event) => { if (event.currentTarget === event.target) setDragging(false); }}
            onDrop={drop}
            className={`rounded-2xl border border-dashed px-5 py-7 text-center transition-colors ${dragging ? "border-accent bg-accent-soft" : "border-line-strong bg-panel-2/45"}`}
          >
            <span className="mx-auto flex h-10 w-10 items-center justify-center rounded-xl bg-panel text-ink-2 shadow-[var(--shadow-card)]" aria-hidden="true">
              <svg viewBox="0 0 20 20" className="h-5 w-5" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M5 2.5h7l3 3V17.5H5z" /><path d="M12 2.5v3h3M7.5 9.5h5M7.5 12.5h5" /></svg>
            </span>
            <p className="mt-3 text-sm font-medium">{file ? file.name : l("拖入模板，或从电脑选择", "Drop the template here or choose one from your computer")}</p>
            <p className="mt-1 text-xs text-ink-3">
              <span className="block">{l("请使用 CareerDesk 表格模板", "Please use the CareerDesk workbook template")}</span>
              <span className="block">{l("单次最多200 条记录，若超过200条，可分批次上传", "Up to 200 records per upload. If you have more than 200 records, upload them in batches.")}</span>
            </p>
            <button type="button" disabled={uploading} onClick={() => inputRef.current?.click()} className="btn btn-sm mt-3">{file ? l("更换文件", "Change file") : l("选择文件", "Choose file")}</button>
            <input ref={inputRef} type="file" accept=".xlsx,.xls,.csv,.tsv" tabIndex={-1} aria-hidden="true" className="sr-only" onChange={(event) => choose(event.target.files?.item(0) ?? null)} />
          </div>

          <button
            type="button"
            disabled={downloading}
            onClick={() => void downloadTemplate()}
            className="mt-3 flex w-full items-center justify-center gap-2 rounded-xl border border-line-strong bg-panel px-4 py-3 text-sm font-semibold text-ink shadow-[var(--shadow-card)] transition-colors hover:border-accent hover:text-accent"
          >
            <svg viewBox="0 0 16 16" className="h-4 w-4" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
              <path d="M8 2.5v7M5.5 7.5 8 10l2.5-2.5M3 12.5h10" />
            </svg>
            {downloading ? l("正在下载…", "Downloading…") : l("下载 CareerDesk 表格模板", "Download CareerDesk workbook template")}
          </button>

          {downloadedPath && (
            <div role="status" className="mt-3 rounded-xl bg-ok-soft px-3 py-2.5 text-xs leading-5 text-ok">
              <div className="flex items-center justify-between gap-3">
                <div className="min-w-0">
                  <p className="font-medium">{l("表格模板", "Workbook template")}</p>
                  <p className="truncate text-ink-2" title={downloadedPath}>{downloadedPath}</p>
                </div>
                <button type="button" className="btn btn-sm shrink-0" onClick={() => void openDownloadedTemplate()}>
                  {l("打开模板", "Open template")}
                </button>
              </div>
            </div>
          )}

          {error && <p role="alert" className="mt-3 rounded-xl bg-bad-soft px-3 py-2.5 text-sm leading-5 text-bad">{error}</p>}
          <div className="mt-5 flex justify-end gap-2">
            <button type="button" onClick={onCancel} disabled={uploading} className="btn">{l("取消", "Cancel")}</button>
            <button type="submit" disabled={!file || uploading} className="btn-primary">{uploading ? l("正在生成预览…", "Preparing preview…") : l("生成导入预览", "Prepare import preview")}</button>
          </div>
        </form>
      </div>
    </div>
  );
}
