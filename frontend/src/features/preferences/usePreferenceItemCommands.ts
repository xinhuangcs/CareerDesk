import { useCallback, useEffect, useRef, useState } from "react";

import { HttpError } from "../../shared/api/transport";
import { useLocalizer } from "../../i18n/useLocalizer";
import { useLocale } from "../../i18n/localePreference";
import { createSecureCommandId } from "../operations/secureCommandId";
import {
  cancelPreferenceItemCommandIfAbsent,
  getPreferenceItemCommand,
  getPreferencesSnapshot,
  putPreferenceItemCommand,
} from "./preferencesApi";
import {
  isPreferenceItemCommandStatus,
  type PreferenceItemCommandPayload,
  type PreferenceItemCommandStatus,
} from "./preferenceItemCommandContract";
import {
  clearPreferenceItemCommandOutbox,
  persistPreferenceItemCommand,
  readPreferenceItemCommandOutbox,
  type PersistedPreferenceItemCommand,
} from "./preferenceItemCommandOutbox";
import {
  PREFERENCE_COMMAND_RETRY_DELAYS_MS,
  reconcilePreferenceItemCommand,
} from "./preferenceItemCommandState";
import { isPreferencesSnapshot, type PreferencesSnapshot } from "./preferencesContract";
import { broadcastPreferenceInvalidation } from "./preferenceInvalidation";

export type PreferenceItemCommandPhase =
  | "submitting"
  | "checking"
  | "awaiting_user"
  | "reconciling"
  | "unknown";

export type ActivePreferenceItemCommand = PersistedPreferenceItemCommand & {
  phase: PreferenceItemCommandPhase;
  canReplay: boolean;
  value?: string;
  detail: string;
};

export type PreferenceItemCommandNotice = {
  kind: "ok" | "bad" | "warn";
  text: string;
};

type StatusRead = { kind: "absent" } | { kind: "terminal"; status: PreferenceItemCommandStatus };

function wait(delayMs: number, signal: AbortSignal): Promise<boolean> {
  if (signal.aborted) return Promise.resolve(false);
  return new Promise((resolve) => {
    const timer = window.setTimeout(() => {
      signal.removeEventListener("abort", onAbort);
      resolve(true);
    }, delayMs);
    const onAbort = () => {
      window.clearTimeout(timer);
      resolve(false);
    };
    signal.addEventListener("abort", onAbort, { once: true });
  });
}

export function usePreferenceItemCommands({
  snapshot,
  onSnapshot,
}: {
  snapshot: PreferencesSnapshot | null;
  onSnapshot: (snapshot: PreferencesSnapshot) => void;
}) {
  const l = useLocalizer();
  const { locale } = useLocale();
  const [activeCommand, setActiveCommand] = useState<ActivePreferenceItemCommand | null>(null);
  const [notice, setNotice] = useState<PreferenceItemCommandNotice | null>(null);
  const [persistenceAvailable, setPersistenceAvailable] = useState(true);
  const [settledStatus, setSettledStatus] = useState<PreferenceItemCommandStatus | null>(null);
  const commandRef = useRef<ActivePreferenceItemCommand | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const initializedScopeRef = useRef<string | null>(null);
  const snapshotRef = useRef(snapshot);
  snapshotRef.current = snapshot;

  const updateActive = useCallback((value: ActivePreferenceItemCommand | null) => {
    commandRef.current = value;
    setActiveCommand(value);
  }, []);

  const readStatus = useCallback(async (
    command: PersistedPreferenceItemCommand,
    signal: AbortSignal,
  ): Promise<StatusRead> => {
    try {
      const candidate = await getPreferenceItemCommand(command.commandId, { signal });
      if (!isPreferenceItemCommandStatus(candidate, command.commandId, command)) {
        throw new Error(l("返回的偏好修改结果无法验证", "The returned preference-change result could not be verified"));
      }
      return { kind: "terminal", status: candidate };
    } catch (reason) {
      if (reason instanceof HttpError && reason.status === 404) return { kind: "absent" };
      throw reason;
    }
  }, [l]);

  const settle = useCallback(async (
    command: ActivePreferenceItemCommand,
    status: PreferenceItemCommandStatus,
    signal: AbortSignal,
  ) => {
    updateActive({ ...command, phase: "reconciling", detail: l("修改已完成，正在同步最新列表…", "Change completed; syncing the latest list…") });
    try {
      const candidate = await getPreferencesSnapshot({ signal });
      if (!isPreferencesSnapshot(candidate)
          || candidate.recovery_scope !== snapshotRef.current?.recovery_scope) {
        throw new Error(l("偏好列表无法验证，请刷新后重试", "The preference list could not be verified. Refresh and try again."));
      }
      const reconciliation = reconcilePreferenceItemCommand(status, candidate, locale);
      if (!reconciliation.valid) throw new Error(reconciliation.message);
      let message = reconciliation.message;
      let kind: PreferenceItemCommandNotice["kind"] = reconciliation.current ? "ok" : "warn";
      if (status.state === "completed"
          && status.result?.outcome === "updated"
          && command.value !== undefined
          && status.result.final !== null) {
        const current = candidate.items.find((item) => item.id === status.result?.final?.id);
        if (current?.revision === status.result.final.revision && current.value !== command.value) {
          kind = "warn";
          message = l("这项偏好已在另一处更新；页面已显示最新值，本地草稿没有覆盖它。", "This preference was updated elsewhere. The page shows the latest value; your local draft did not overwrite it.");
        }
      }
      onSnapshot(candidate);
      if (!clearPreferenceItemCommandOutbox(candidate.recovery_scope, command.commandId)) {
        throw new Error(l("修改已完成，但浏览器无法清理本地恢复信息", "The change completed, but the browser could not clear local recovery information"));
      }
      updateActive(null);
      setSettledStatus(status);
      setNotice({
        kind: status.state === "rejected" ? "bad" : status.state === "cancelled" ? "warn" : kind,
        text: message,
      });
      if (status.state === "completed" && status.result?.outcome !== "no_change") {
        broadcastPreferenceInvalidation(candidate.recovery_scope);
      }
    } catch (reason) {
      if (signal.aborted) return;
      const text = reason instanceof Error ? reason.message : l("当前偏好列表核对失败", "Could not verify the current preference list");
      const next = { ...command, phase: "unknown" as const, detail: l(`${text}；本地恢复记录仍保留。`, `${text}; local recovery information is retained.`) };
      updateActive(next);
      setNotice({ kind: "bad", text: next.detail });
    }
  }, [l, locale, onSnapshot, updateActive]);

  const poll = useCallback(async (
    command: ActivePreferenceItemCommand,
    payload: PreferenceItemCommandPayload | null,
  ) => {
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    const signal = controller.signal;
    let lastDetail = l("系统尚未记录本次修改。", "The system has not recorded this change yet.");
    updateActive({
      ...command,
      phase: payload === null ? "checking" : "submitting",
      detail: payload === null ? l("正在核对上次未确认的修改…", "Checking the last unconfirmed change…") : l("正在提交并核对修改结果…", "Submitting and verifying the change…"),
    });
    try {
      for (let attempt = 0; attempt <= PREFERENCE_COMMAND_RETRY_DELAYS_MS.length; attempt += 1) {
        if (signal.aborted) return;
        if (payload !== null) {
          try {
            const candidate = await putPreferenceItemCommand(command.commandId, payload, { signal });
            if (!isPreferenceItemCommandStatus(candidate, command.commandId, command)) {
              throw new Error(l("返回的偏好修改结果无法验证", "The returned preference-change result could not be verified"));
            }
            await settle({
              ...command,
              value: payload.action === "set" ? payload.value : undefined,
            }, candidate, signal);
            return;
          } catch (reason) {
            if (signal.aborted) return;
            lastDetail = reason instanceof Error ? reason.message : l("偏好修改是否提交成功仍待确认", "Whether the preference change was submitted is still unconfirmed");
          }
        }
        try {
          const result = await readStatus(command, signal);
          if (result.kind === "terminal") {
            await settle(command, result.status, signal);
            return;
          }
          lastDetail = l("系统尚未记录本次修改。", "The system has not recorded this change yet.");
        } catch (reason) {
          if (signal.aborted) return;
          lastDetail = reason instanceof Error ? reason.message : l("偏好修改状态读取失败", "Could not read preference-change status");
        }
        if (attempt === PREFERENCE_COMMAND_RETRY_DELAYS_MS.length) break;
        updateActive({
          ...command,
          phase: "checking",
          detail: l(`结果仍待确认，正在安全核对（${attempt + 1}/${PREFERENCE_COMMAND_RETRY_DELAYS_MS.length + 1}）…`, `Result still unconfirmed; checking safely (${attempt + 1}/${PREFERENCE_COMMAND_RETRY_DELAYS_MS.length + 1})…`),
        });
        if (!await wait(PREFERENCE_COMMAND_RETRY_DELAYS_MS[attempt], signal)) return;
      }
      const next = {
        ...command,
        phase: "awaiting_user" as const,
        detail: l(`${lastDetail} 你可以继续核对，或停止等待；系统会阻止迟到的请求继续修改。`, `${lastDetail} You can keep checking or stop waiting; late requests will be prevented from changing the preference.`),
      };
      updateActive(next);
      setNotice({ kind: "warn", text: next.detail });
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
    }
  }, [l, readStatus, settle, updateActive]);

  useEffect(() => () => abortRef.current?.abort(), []);

  useEffect(() => {
    const scope = snapshot?.recovery_scope;
    if (!scope || initializedScopeRef.current === scope) return;
    abortRef.current?.abort();
    updateActive(null);
    initializedScopeRef.current = scope;
    setSettledStatus(null);
    const recovered = readPreferenceItemCommandOutbox(scope);
    if (recovered.state === "unavailable") {
      setPersistenceAvailable(false);
      setNotice({
        kind: "bad",
        text: l("浏览器的修改恢复功能不可用；当前仍可查看偏好，但不会提交编辑或删除。", "Browser recovery for changes is unavailable. You can still view preferences, but edits and deletions will not be submitted."),
      });
      return;
    }
    setPersistenceAvailable(true);
    if (recovered.state === "corrupt") {
      setNotice({
        kind: "bad",
        text: l("本地偏好恢复信息已损坏并停止使用；请核对当前列表。", "Local preference recovery information is corrupted and has been disabled. Review the current list."),
      });
      return;
    }
    if (recovered.state === "pending") {
      const command = {
        ...recovered.command,
        canReplay: false,
        phase: "checking" as const,
        detail: l("正在确认上次未完成的修改…", "Confirming the last incomplete change…"),
      };
      updateActive(command);
      void poll(command, null);
    }
  }, [poll, snapshot?.recovery_scope, updateActive]);

  const submit = useCallback((payload: PreferenceItemCommandPayload): boolean => {
    const currentSnapshot = snapshotRef.current;
    if (!currentSnapshot || commandRef.current !== null || !persistenceAvailable) return false;
    const commandId = createSecureCommandId();
    if (commandId === null) {
      setNotice({ kind: "bad", text: l("浏览器无法创建本次修改所需的安全编号，因此没有提交。", "The browser could not create a secure identifier for this change, so nothing was submitted.") });
      return false;
    }
    const persisted: PersistedPreferenceItemCommand = {
      commandId,
      action: payload.action,
      target: payload.target,
      createdAt: Date.now(),
    };
    if (!persistPreferenceItemCommand(currentSnapshot.recovery_scope, persisted)) {
      setPersistenceAvailable(false);
      setNotice({
        kind: "bad",
        text: l("浏览器无法保存修改恢复信息，因此本次没有提交；请检查隐私模式或存储空间。", "The browser could not save recovery information, so nothing was submitted. Check private-browsing settings or available storage."),
      });
      return false;
    }
    const command = {
      ...persisted,
      value: payload.action === "set" ? payload.value : undefined,
      canReplay: true,
      phase: "submitting" as const,
      detail: l("正在提交…", "Submitting…"),
    };
    updateActive(command);
    setSettledStatus(null);
    setNotice(null);
    void poll(command, payload);
    return true;
  }, [l, persistenceAvailable, poll, updateActive]);

  const continueChecking = useCallback(() => {
    const command = commandRef.current;
    if (command === null) return;
    const payload = !command.canReplay
      ? null
      : command.action === "delete"
        ? { action: "delete" as const, target: command.target }
        : command.value === undefined
          ? null
          : { action: "set" as const, target: command.target, value: command.value };
    void poll(command, payload);
  }, [poll]);

  const safeStop = useCallback(async () => {
    const command = commandRef.current;
    if (command === null || command.phase !== "awaiting_user") return;
    abortRef.current?.abort();
    const controller = new AbortController();
    abortRef.current = controller;
    updateActive({ ...command, phase: "checking", detail: l("正在安全停止等待…", "Stopping the wait safely…") });
    try {
      const candidate = await cancelPreferenceItemCommandIfAbsent(
        command.commandId,
        { action: command.action, target: command.target },
        { signal: controller.signal },
      );
      if (!isPreferenceItemCommandStatus(candidate, command.commandId, command)) {
        throw new Error(l("返回的停止结果无法验证", "The returned stop result could not be verified"));
      }
      await settle(command, candidate, controller.signal);
    } catch (reason) {
      if (controller.signal.aborted) return;
      const detail = reason instanceof Error ? reason.message : l("安全停止结果未确认", "Safe-stop result is unconfirmed");
      const next = { ...command, phase: "awaiting_user" as const, detail: l(`${detail}；恢复记录仍保留。`, `${detail}; recovery information is retained.`) };
      updateActive(next);
      setNotice({ kind: "warn", text: next.detail });
    } finally {
      if (abortRef.current === controller) abortRef.current = null;
    }
  }, [l, settle, updateActive]);

  return {
    activeCommand,
    notice,
    persistenceAvailable,
    settledStatus,
    setNotice,
    submit,
    continueChecking,
    safeStop,
  };
}
