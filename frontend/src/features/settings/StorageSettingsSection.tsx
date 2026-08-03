import { useEffect, useState } from "react";
import { HttpError } from "../../shared/api/transport";
import { desktopApi } from "../../shared/native/desktopBridge";
import { useLocalizer } from "../../i18n/useLocalizer";
import {
  cancelStorageMigration,
  loadStorageLocation,
  requestStorageMigration,
  revealStorageLocation,
  type StorageLocationState,
} from "./storageApi";

const PATH_ROWS = [
  ["业务数据", "Business data", "data_dir", "data"],
  ["配置", "Configuration", "config_dir", "config"],
  ["运行日志", "Runtime logs", "log_dir", "logs"],
] as const;

export function StorageSettingsSection() {
  const l = useLocalizer();
  const [state, setState] = useState<StorageLocationState | null>(null);
  const [destination, setDestination] = useState("");
  const [busy, setBusy] = useState(false);
  const [nativePicker, setNativePicker] = useState(Boolean(desktopApi()));
  const [notice, setNotice] = useState<{ kind: "ok" | "bad"; text: string } | null>(null);

  useEffect(() => {
    loadStorageLocation()
      .then(setState)
      .catch((error: unknown) => setNotice({
        kind: "bad",
        text: error instanceof Error ? error.message : l("存储位置读取失败。", "Could not read storage locations."),
      }));
  }, []);

  useEffect(() => {
    const ready = () => setNativePicker(Boolean(desktopApi()));
    window.addEventListener("pywebviewready", ready);
    return () => window.removeEventListener("pywebviewready", ready);
  }, []);

  async function chooseDirectory() {
    try {
      const selected = await desktopApi()?.select_data_directory?.();
      if (selected) setDestination(selected);
    } catch (error) {
      setNotice({
        kind: "bad",
        text: error instanceof Error ? error.message : l("无法打开文件夹选择器。", "Could not open the folder picker."),
      });
    }
  }

  async function reveal(target: "data" | "config" | "logs") {
    setNotice(null);
    try {
      setState(await revealStorageLocation(target));
    } catch (error) {
      setNotice({
        kind: "bad",
        text: error instanceof Error ? error.message : l("无法打开目录。", "Could not open the directory."),
      });
    }
  }

  async function migrate() {
    if (!destination.trim()) {
      setNotice({ kind: "bad", text: l("请填写一个尚不存在的专用目录，例如 ~/Documents/CareerDesk Data。", "Enter a dedicated directory that does not yet exist, such as ~/Documents/CareerDesk Data.") });
      return;
    }
    setBusy(true);
    setNotice(null);
    try {
      const next = await requestStorageMigration(destination.trim());
      setState(next);
      setDestination("");
      setNotice({
        kind: "ok",
        text: l("迁移已准备。请关闭 CareerDesk 窗口；应用会在服务完全停止后校验、复制并切换。完成后重新打开即可，原目录不会删除。", "Migration is prepared. Close the CareerDesk window; after the service fully stops, it will verify, copy, and switch. Reopen when complete. The original directory will not be deleted."),
      });
    } catch (error) {
      setNotice({
        kind: "bad",
        text: error instanceof HttpError ? error.message : error instanceof Error ? error.message : l("迁移准备失败。", "Could not prepare migration."),
      });
    } finally {
      setBusy(false);
    }
  }

  async function cancelMigration() {
    setBusy(true);
    setNotice(null);
    try {
      setState(await cancelStorageMigration());
      setNotice({
        kind: "ok",
        text: l("迁移请求已取消；当前数据目录和已生成的任何数据副本都未删除。", "Migration was cancelled. Neither the current data directory nor any generated copy was deleted."),
      });
    } catch (error) {
      setNotice({
        kind: "bad",
        text: error instanceof Error ? error.message : l("迁移请求取消失败。", "Could not cancel migration."),
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="card p-5" aria-labelledby="storage-settings-title">
      <h2 id="storage-settings-title" className="text-sm font-semibold">{l("本机数据", "Local data")}</h2>
      <p className="mt-1 text-xs leading-relaxed text-ink-3">
        {l("所有业务数据、配置和日志仅保存在你的这台电脑上，请定期备份重要数据。", "All business data, configuration, and logs are stored only on this computer. Back up important data regularly.")}
        <br />
        {l("卸载不会自动删除这些数据，下载安装更新后的版本后会自动识别原有数据。", "Uninstalling does not automatically delete this data, and an updated installation will detect it automatically.")}
      </p>
      {state && (
        <>
          <div className="mt-4 flex flex-col gap-3">
            {PATH_ROWS.map(([zhLabel, enLabel, key, target]) => (
              <div key={key} className="rounded-xl border border-line bg-panel-2 p-3">
                <div className="flex flex-wrap items-center justify-between gap-2">
                  <span className="text-xs font-medium text-ink">{l(zhLabel, enLabel)}</span>
                  <button type="button" className="btn btn-sm" onClick={() => void reveal(target)}>
                    {l("在文件管理器中打开", "Open in file manager")}
                  </button>
                </div>
                <code className="mt-1.5 block break-all text-xs text-ink-2">{state[key]}</code>
              </div>
            ))}
          </div>
          <p className="mt-3 text-xs leading-relaxed text-ink-3">
            {l("API Key 保存位置：", "API key location: ")}<span className="break-all text-ink-2">{state.credential_location}</span>
            {state.credential_storage_kind === "system"
              ? l("。Key 不在 .app、业务数据库、备份或上述配置目录中。", ". Keys are not stored in the .app, business database, backups, or configuration directories above.")
              : state.credential_storage_kind === "server_environment"
                ? l("。Key 未写入 .app、业务数据库或上述目录；请在启动 CareerDesk 的环境中管理。", ". Keys are not written to the .app, business database, or directories above; manage them in CareerDesk's startup environment.")
                : l("。源码运行模式的私有配置文件可能包含 Key，请勿分享。", ". The private configuration file used in source mode may contain keys; do not share it.")}
          </p>

          <div className="mt-5 border-t border-line pt-4">
            <h3 className="text-sm font-medium text-ink">{l("更改数据保存位置", "Change data location")}</h3>
            <p className="mt-1 text-xs leading-relaxed text-ink-3">
              {l("建议保留默认位置。如需更改，请选择一个尚不存在的专用文件夹。不建议放进网盘同步目录，以免多台设备同时修改造成损坏。", "Keeping the default is recommended. If you change it, choose a dedicated folder that does not yet exist. Avoid cloud-synced folders because concurrent changes from multiple devices can corrupt data.")}
            </p>
            {state.migration_pending ? (
              <div className="mt-3 rounded-xl bg-warn-soft p-3 text-xs leading-relaxed text-warn">
                <p>{l("等待关闭应用后迁移到：", "Waiting for the app to close before migrating to:")}</p>
                <code className="mt-1 block break-all">{state.migration_pending}</code>
                <p className="mt-1">{l("请关闭当前 CareerDesk 窗口，等待片刻后重新打开。", "Close the current CareerDesk window, wait briefly, then reopen it.")}</p>
                <button type="button" className="btn btn-sm mt-2" disabled={busy} onClick={() => void cancelMigration()}>
                  {l("取消迁移请求", "Cancel migration")}
                </button>
              </div>
            ) : state.can_customize ? (
              <div className="mt-3 flex flex-col gap-2 sm:flex-row">
                <input
                  aria-label={l("新的业务数据目录", "New business data directory")}
                  className="input min-w-0 flex-1"
                  value={destination}
                  disabled={busy}
                  onChange={(event) => setDestination(event.target.value)}
                  placeholder={l("例如 ~/Documents/CareerDesk Data", "For example, ~/Documents/CareerDesk Data")}
                />
                {nativePicker && (
                  <button type="button" className="btn shrink-0" disabled={busy} onClick={() => void chooseDirectory()}>
                    {l("选择上级文件夹", "Choose parent folder")}
                  </button>
                )}
                <button type="button" className="btn-primary shrink-0" disabled={busy} onClick={() => void migrate()}>
                  {busy ? l("正在准备…", "Preparing…") : l("迁移数据", "Migrate data")}
                </button>
              </div>
            ) : (
              <p className="mt-3 rounded-xl bg-panel-2 p-3 text-xs text-ink-3">
                {state.customization_issue}
              </p>
            )}
            {state.migration_issue && (
              <p role="alert" className="mt-3 rounded-xl bg-warn-soft p-3 text-xs leading-relaxed text-warn">
                {state.migration_issue}
              </p>
            )}
          </div>
        </>
      )}
      {notice && (
        <p
          role={notice.kind === "ok" ? "status" : "alert"}
          className={`mt-3 text-xs leading-relaxed ${notice.kind === "ok" ? "text-ok" : "text-bad"}`}
        >
          {notice.text}
        </p>
      )}
    </section>
  );
}
