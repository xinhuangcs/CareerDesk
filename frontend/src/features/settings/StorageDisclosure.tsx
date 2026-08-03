import { useEffect, useState } from "react";
import { Link } from "react-router-dom";
import { APP_ROUTE_PATHS } from "../../app/routePaths";
import { useLocalizer } from "../../i18n/useLocalizer";
import {
  claimStorageDisclosure,
  loadStorageLocation,
  type StorageLocationState,
} from "./storageApi";

const ACKNOWLEDGEMENT_KEY = "careerdesk.storage-disclosure.v1";

function alreadyAcknowledged(): boolean {
  try {
    return localStorage.getItem(ACKNOWLEDGEMENT_KEY) === "acknowledged";
  } catch {
    return false;
  }
}

export function StorageDisclosure({ hidden = false }: { hidden?: boolean }) {
  const l = useLocalizer();
  const [state, setState] = useState<StorageLocationState | null>(null);
  const [visible, setVisible] = useState(false);

  useEffect(() => {
    if (hidden) return;
    const legacyAcknowledged = alreadyAcknowledged();
    loadStorageLocation()
      .then(async (location) => {
        const claim = await claimStorageDisclosure();
        try {
          localStorage.setItem(ACKNOWLEDGEMENT_KEY, "acknowledged");
        } catch {
          // The durable server-side claim is authoritative when browser storage is unavailable.
        }
        if (claim.should_show && !legacyAcknowledged) {
          setState(location);
          setVisible(true);
        }
      })
      .catch(() => {});
  }, [hidden]);

  if (hidden || !visible || !state) return null;

  function acknowledge() {
    setVisible(false);
  }

  return (
    <div className="mb-6 rounded-2xl border border-accent/30 bg-panel p-4 text-sm" role="dialog" aria-labelledby="storage-disclosure-title">
      <p id="storage-disclosure-title" className="font-medium text-ink">{l("你的数据保存在这台电脑上", "Your data is stored on this computer")}</p>
      <p className="mt-1 leading-relaxed text-ink-2">
        {l("业务数据默认保存在", "Business data is stored by default in")} <code className="break-all text-xs">{state.data_dir}</code>{l("；API Key 保存在 ", "; API keys are stored in ")}{state.credential_location}{l("。", ". ")}{l("卸载或分享应用本身不会带走这些数据，也不会自动删除它们。", "Uninstalling or sharing the app itself neither includes nor automatically deletes this data.")}
      </p>
      <div className="mt-3 flex flex-wrap items-center gap-2">
        <button type="button" className="btn-primary" onClick={acknowledge}>{l("知道了", "Got it")}</button>
        <Link to={APP_ROUTE_PATHS.settings} className="btn">{l("查看存储位置", "View storage locations")}</Link>
      </div>
    </div>
  );
}
