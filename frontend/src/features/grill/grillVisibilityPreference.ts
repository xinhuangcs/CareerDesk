const GRILL_VISIBILITY_KEY = "careerdesk.grill.navigation.v1"; // gitleaks:allow -- public localStorage identifier
const GRILL_INTRO_KEY_PREFIX = "careerdesk.grill.experiment-intro.v2";
export const GRILL_VISIBILITY_EVENT = "careerdesk:grill-visibility-change";

type ReadableStorage = Pick<Storage, "getItem">;
type WritableStorage = Pick<Storage, "setItem">;

export function grillNavigationIsVisible(
  storage: ReadableStorage = window.localStorage,
): boolean {
  try {
    return storage.getItem(GRILL_VISIBILITY_KEY) !== "hidden";
  } catch {
    return true;
  }
}

export function saveGrillNavigationVisibility(
  visible: boolean,
  storage: WritableStorage = window.localStorage,
  notify: () => void = () => window.dispatchEvent(new Event(GRILL_VISIBILITY_EVENT)),
): boolean {
  try {
    storage.setItem(GRILL_VISIBILITY_KEY, visible ? "visible" : "hidden");
    notify();
    return true;
  } catch {
    return false;
  }
}

export function grillExperimentIntroWasSeen(
  releaseVersion: string,
  storage: ReadableStorage = window.localStorage,
): boolean {
  try {
    return storage.getItem(`${GRILL_INTRO_KEY_PREFIX}:${encodeURIComponent(releaseVersion)}`) === "seen";
  } catch {
    return false;
  }
}

export function markGrillExperimentIntroSeen(
  releaseVersion: string,
  storage: WritableStorage = window.localStorage,
): boolean {
  try {
    storage.setItem(`${GRILL_INTRO_KEY_PREFIX}:${encodeURIComponent(releaseVersion)}`, "seen");
    return true;
  } catch {
    return false;
  }
}

export function subscribeToGrillVisibility(
  listener: (visible: boolean) => void,
  runtime: Pick<Window, "addEventListener" | "removeEventListener" | "localStorage"> = window,
): () => void {
  const refresh = () => listener(grillNavigationIsVisible(runtime.localStorage));
  runtime.addEventListener(GRILL_VISIBILITY_EVENT, refresh);
  runtime.addEventListener("storage", refresh);
  return () => {
    runtime.removeEventListener(GRILL_VISIBILITY_EVENT, refresh);
    runtime.removeEventListener("storage", refresh);
  };
}
