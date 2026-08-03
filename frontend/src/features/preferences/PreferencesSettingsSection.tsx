import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { formatDate } from "../../i18n/formatters";
import { useLocale } from "../../i18n/localePreference";
import { useLocalizer } from "../../i18n/useLocalizer";

import { getPreferencesSnapshot } from "./preferencesApi";
import {
  preferenceValueCodePointLength,
  preferenceValueValidationIssue,
} from "./preferenceItemCommandState";
import {
  isPreferencesSnapshot,
  type PreferenceItem,
  type PreferencesSnapshot,
} from "./preferencesContract";
import { subscribePreferenceInvalidation } from "./preferenceInvalidation";
import { usePreferenceItemCommands } from "./usePreferenceItemCommands";

type Editor = {
  id: number;
  baselineRevision: number;
  draft: string;
};

type DeleteConfirmation = {
  id: number;
  baselineRevision: number;
};

export function PreferencesSettingsSection() {
  const l = useLocalizer();
  const { locale } = useLocale();
  const [refreshSignal, setRefreshSignal] = useState(0);
  const [snapshot, setSnapshot] = useState<PreferencesSnapshot | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [editor, setEditor] = useState<Editor | null>(null);
  const [deleteConfirmation, setDeleteConfirmation] = useState<DeleteConfirmation | null>(null);
  const titleRef = useRef<HTMLHeadingElement | null>(null);

  const acceptSnapshot = useCallback((value: PreferencesSnapshot) => {
    setSnapshot(value);
    setError("");
  }, []);
  const commands = usePreferenceItemCommands({ snapshot, onSnapshot: acceptSnapshot });
  const commandBusy = commands.activeCommand !== null
    && commands.activeCommand.phase !== "awaiting_user"
    && commands.activeCommand.phase !== "unknown";

  useEffect(() => {
    const controller = new AbortController();
    setLoading(true);
    setError("");
    void getPreferencesSnapshot({ signal: controller.signal })
      .then((value) => {
        if (!isPreferencesSnapshot(value)) {
          throw new Error(l("偏好列表不完整或无法验证，请刷新后重试", "The preference list is incomplete or could not be verified. Refresh and try again."));
        }
        acceptSnapshot(value);
      })
      .catch((reason: unknown) => {
        if (reason instanceof DOMException && reason.name === "AbortError") return;
        setError(reason instanceof Error ? reason.message : l("偏好读取失败", "Could not load preferences"));
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [acceptSnapshot, l, refreshSignal]);

  useEffect(() => {
    if (!snapshot) return;
    return subscribePreferenceInvalidation(snapshot.recovery_scope, () => {
      if (commands.activeCommand === null) setRefreshSignal((current) => current + 1);
    });
  }, [commands.activeCommand, snapshot]);

  useEffect(() => {
    const refreshWhenVisible = () => {
      if (!document.hidden && commands.activeCommand === null) {
        setRefreshSignal((current) => current + 1);
      }
    };
    window.addEventListener("focus", refreshWhenVisible);
    document.addEventListener("visibilitychange", refreshWhenVisible);
    return () => {
      window.removeEventListener("focus", refreshWhenVisible);
      document.removeEventListener("visibilitychange", refreshWhenVisible);
    };
  }, [commands.activeCommand]);

  useEffect(() => {
    const status = commands.settledStatus;
    if (status === null) return;
    if (status.state === "completed") {
      setEditor((current) => current?.id === status.target.id ? null : current);
      setDeleteConfirmation((current) => current?.id === status.target.id ? null : current);
    }
    requestAnimationFrame(() => titleRef.current?.focus());
  }, [commands.settledStatus]);

  const itemById = useMemo(
    () => new Map(snapshot?.items.map((item) => [item.id, item]) ?? []),
    [snapshot],
  );
  const editorItem = editor ? itemById.get(editor.id) : undefined;
  const editorStale = editor !== null
    && (editorItem === undefined || editorItem.revision !== editor.baselineRevision);
  const deleteItem = deleteConfirmation ? itemById.get(deleteConfirmation.id) : undefined;
  const deleteStale = deleteConfirmation !== null
    && (deleteItem === undefined || deleteItem.revision !== deleteConfirmation.baselineRevision);
  const writesDisabled = loading || error !== "" || commandBusy
    || commands.activeCommand !== null || !commands.persistenceAvailable;

  const beginEdit = (item: PreferenceItem) => {
    setDeleteConfirmation(null);
    setEditor({ id: item.id, baselineRevision: item.revision, draft: item.value });
    commands.setNotice(null);
  };

  const focusItemAction = (id: number, action: "edit" | "delete") => {
    requestAnimationFrame(() => {
      document.getElementById(`preference-item-${action}-${id}`)?.focus();
    });
  };

  const submitEdit = () => {
    if (!editor || !editorItem || editorStale) return;
    const issue = preferenceValueValidationIssue(editor.draft, editorItem.value, locale);
    if (issue !== null) {
      commands.setNotice({ kind: "bad", text: issue });
      return;
    }
    commands.submit({
      action: "set",
      target: { id: editorItem.id, revision: editor.baselineRevision },
      value: editor.draft,
    });
  };

  const submitDelete = () => {
    if (!deleteConfirmation || !deleteItem || deleteStale) return;
    commands.submit({
      action: "delete",
      target: { id: deleteItem.id, revision: deleteConfirmation.baselineRevision },
    });
  };

  return (
    <section
      className="card overflow-hidden"
      aria-labelledby="preferences-settings-title"
      aria-busy={loading || commandBusy}
    >
      <div className="flex flex-wrap items-start justify-between gap-3 border-b border-line px-5 py-4">
        <div>
          <h2
            ref={titleRef}
            tabIndex={-1}
            id="preferences-settings-title"
            className="text-sm font-semibold outline-none"
          >
            {l("长期偏好", "Long-term preferences")}
          </h2>
          <p className="mt-1 text-xs leading-relaxed text-ink-3">
            {l("管理求职助手记住的关于您的求职方向、城市和薪资等长期偏好。点击每项右侧的“手动编辑”即可修改，内容只保存在本机。", "Manage the long-term preferences the career assistant remembers, such as career direction, cities, and compensation. Choose Edit manually beside an item to change it. Content stays on this device.")}
          </p>
        </div>
        <button
          type="button"
          className="btn btn-sm shrink-0"
          disabled={loading || commands.activeCommand !== null}
          onClick={() => setRefreshSignal((current) => current + 1)}
        >
          {loading ? l("读取中…", "Loading…") : l("刷新", "Refresh")}
        </button>
      </div>

      <div className="px-5 py-4">
        {loading && snapshot === null && (
          <p role="status" className="text-sm text-ink-3">{l("正在读取长期偏好…", "Loading long-term preferences…")}</p>
        )}
        {error && (
          <div role="alert" className="rounded-xl bg-bad-soft px-3 py-2.5 text-sm text-bad">
            <p>
              {error}{l("。", ". ")}{snapshot
                ? l("下方仅保留上一次已确认快照，当前状态未能核对，写入已关闭。", "Only the last verified snapshot is shown below. The current state could not be checked, so writes are disabled.")
                : l("页面不会用旧缓存冒充当前值。", "The page will not present stale cached data as current.")}
            </p>
            <button
              type="button"
              className="btn btn-sm mt-2"
              onClick={() => setRefreshSignal((current) => current + 1)}
            >
              {l("重新读取", "Reload")}
            </button>
          </div>
        )}

        {commands.notice && (
          <p
            role={commands.notice.kind === "bad" ? "alert" : "status"}
            className={`mt-3 rounded-xl px-3 py-2.5 text-sm ${
              commands.notice.kind === "bad"
                ? "bg-bad-soft text-bad"
                : commands.notice.kind === "warn"
                  ? "bg-warn-soft text-warn"
                  : "bg-ok-soft text-ok"
            }`}
          >
            {commands.notice.text}
          </p>
        )}

        {commands.activeCommand && (
          <div className="mt-3 rounded-xl border border-warn/30 bg-warn-soft px-3 py-3 text-sm text-warn">
            <p role="status">{commands.activeCommand.detail}</p>
            {(commands.activeCommand.phase === "awaiting_user"
                || commands.activeCommand.phase === "unknown") && (
              <div className="mt-2 flex flex-wrap gap-2">
                <button type="button" className="btn btn-sm" onClick={commands.continueChecking}>
                  {l("继续核对", "Keep checking")}
                </button>
                {commands.activeCommand.phase === "awaiting_user" && (
                  <button type="button" className="btn btn-sm" onClick={() => void commands.safeStop()}>
                    {l("安全停止等待", "Stop waiting safely")}
                  </button>
                )}
              </div>
            )}
          </div>
        )}

        {!loading && snapshot?.items.length === 0 && !error && (
          <div className="mt-3 rounded-xl bg-panel-2 px-3 py-3 text-sm text-ink-2">
            <p>{l("还没有保存长期偏好。", "No long-term preferences are saved yet.")}</p>
            <p className="mt-1 text-xs text-ink-3">{l("你可以在求职助手中告诉 CareerDesk 要记住什么。", "Tell CareerDesk what to remember in the career assistant.")}</p>
          </div>
        )}

        {snapshot && snapshot.items.length > 0 && (
          <div className="mt-3">
            <p className="mb-3 text-xs text-ink-3">
              {l(`共 ${snapshot.total} 项 · ${snapshot.total_chars} 个字符`, `${snapshot.total} items · ${snapshot.total_chars} characters`)}
              {loading || error ? l(" · 正在重新核对", " · Rechecking") : l(" · 已与本机记录同步", " · Synced with local records")}
            </p>
            <ul className="divide-y divide-line rounded-xl border border-line">
              {snapshot.items.map((item) => {
                const editing = editor?.id === item.id;
                const confirmingDelete = deleteConfirmation?.id === item.id;
                const valueIssue = editing
                  ? preferenceValueValidationIssue(editor.draft, item.value, locale)
                  : null;
                const itemBusy = commands.activeCommand?.target.id === item.id;
                const descriptionId = `preference-item-description-${item.id}`;
                const editorId = `preference-item-editor-${item.id}`;
                return (
                  <li key={item.id} className="px-3 py-3" aria-busy={itemBusy}>
                    <div className="flex flex-wrap items-start justify-between gap-3">
                      <div className="min-w-0 flex-1">
                        <div className="flex flex-wrap items-baseline justify-between gap-2">
                          <h3 className="break-words text-sm font-medium text-ink">{item.key}</h3>
                          <time dateTime={item.updated_time} className="text-xs tabular-nums text-ink-3">
                            {l(`版本 ${item.revision}`, `Revision ${item.revision}`)} · {formatDate(item.updated_time, locale, "dateTime")}
                          </time>
                        </div>
                        {!editing && (
                          <p
                            id={descriptionId}
                            className="mt-1 whitespace-pre-wrap break-words text-sm leading-relaxed text-ink-2"
                          >
                            {item.value}
                          </p>
                        )}
                      </div>
                      {!editing && !confirmingDelete && (
                        <div className="flex shrink-0 gap-2">
                          <button
                            id={`preference-item-edit-${item.id}`}
                            type="button"
                            className="btn btn-sm"
                            disabled={writesDisabled}
                            aria-describedby={descriptionId}
                            onClick={() => beginEdit(item)}
                          >
                            {l("手动编辑", "Edit manually")}
                          </button>
                          <button
                            id={`preference-item-delete-${item.id}`}
                            type="button"
                            className="btn btn-sm text-bad"
                            disabled={writesDisabled}
                            aria-describedby={descriptionId}
                            onClick={() => {
                              setEditor(null);
                              setDeleteConfirmation({ id: item.id, baselineRevision: item.revision });
                              commands.setNotice(null);
                            }}
                          >
                            {l("删除", "Delete")}
                          </button>
                        </div>
                      )}
                    </div>

                    {editing && editor && (
                      <form
                        className="mt-3 rounded-lg bg-panel-2 p-3"
                        onSubmit={(event) => {
                          event.preventDefault();
                          submitEdit();
                        }}
                      >
                        <label htmlFor={editorId} className="text-xs font-medium text-ink-2">
                          {l(`修改“${item.key}”的值`, `Edit the value of “${item.key}”`)}
                        </label>
                        <textarea
                          id={editorId}
                          className="input mt-1.5 min-h-24 w-full resize-y"
                          value={editor.draft}
                          disabled={commands.activeCommand !== null}
                          aria-describedby={`${editorId}-count ${editorId}-warning`}
                          autoFocus
                          onChange={(event) => setEditor({ ...editor, draft: event.target.value })}
                        />
                        <div className="mt-1 flex flex-wrap items-start justify-between gap-2 text-xs">
                          <span id={`${editorId}-warning`} className={editorStale ? "text-bad" : "text-ink-3"}>
                            {editorStale
                              ? l("这项偏好已在其他页面被修改或删除。草稿仍在，请刷新并核对后再保存。", "This preference was changed or deleted on another page. Your draft remains; refresh and review it before saving.")
                              : valueIssue
                                ? valueIssue
                                : l("保存会替换当前内容且无法撤销。系统只在本机保留这项偏好的名称和操作记录，不会把旧内容复制到操作记录中。", "Saving replaces the current content and cannot be undone. Only the preference name and an operation record remain locally; old content is not copied into that record.")}
                          </span>
                          <span
                            id={`${editorId}-count`}
                            className={preferenceValueCodePointLength(editor.draft) > 2_000 ? "text-bad" : "text-ink-3"}
                          >
                            {preferenceValueCodePointLength(editor.draft)} / 2000
                          </span>
                        </div>
                        <div className="mt-3 flex flex-wrap gap-2">
                          <button
                            type="submit"
                            className="btn-primary btn-sm"
                            disabled={writesDisabled || editorStale || valueIssue !== null}
                          >
                            {l("保存修改", "Save changes")}
                          </button>
                          <button
                            type="button"
                            className="btn btn-sm"
                            disabled={commands.activeCommand !== null}
                            onClick={() => {
                              setEditor(null);
                              focusItemAction(item.id, "edit");
                            }}
                          >
                            {l("取消", "Cancel")}
                          </button>
                          {editorStale && editorItem && (
                            <button
                              type="button"
                              className="btn btn-sm"
                              disabled={commands.activeCommand !== null}
                              onClick={() => setEditor({
                                id: editorItem.id,
                                baselineRevision: editorItem.revision,
                                draft: editorItem.value,
                              })}
                            >
                              {l("载入当前值", "Load current value")}
                            </button>
                          )}
                        </div>
                      </form>
                    )}

                    {confirmingDelete && deleteConfirmation && (
                      <fieldset className="mt-3 rounded-lg border border-bad/30 bg-bad-soft p-3">
                        <legend className="px-1 text-xs font-medium text-bad">{l(`确认删除“${item.key}”`, `Confirm deletion of “${item.key}”`)}</legend>
                        <p className="text-xs leading-relaxed text-bad">
                          {l("删除后不能撤销。系统只在本机保留这项偏好的名称和删除记录，不会把已删除的内容复制到操作记录或浏览器临时恢复信息中。", "Deletion cannot be undone. Only the preference name and deletion record remain locally; deleted content is not copied into operation records or browser recovery data.")}
                        </p>
                        {deleteStale && (
                          <p role="alert" className="mt-2 text-xs text-bad">
                            {l("此偏好已变化，旧确认已失效；请取消后重新核对。", "This preference changed, so the previous confirmation is invalid. Cancel and review it again.")}
                          </p>
                        )}
                        <div className="mt-3 flex flex-wrap gap-2">
                          <button
                            type="button"
                            className="btn-primary btn-sm"
                            autoFocus
                            disabled={writesDisabled || deleteStale}
                            onClick={submitDelete}
                          >
                            {l("确认删除这一项", "Delete this item")}
                          </button>
                          <button
                            type="button"
                            className="btn btn-sm"
                            disabled={commands.activeCommand !== null}
                            onClick={() => {
                              setDeleteConfirmation(null);
                              focusItemAction(item.id, "delete");
                            }}
                          >
                            {l("保留", "Keep")}
                          </button>
                        </div>
                      </fieldset>
                    )}
                  </li>
                );
              })}
            </ul>
          </div>
        )}
      </div>

      <div aria-live="polite" className="sr-only">
        {commands.activeCommand?.detail ?? commands.notice?.text ?? ""}
      </div>
    </section>
  );
}
