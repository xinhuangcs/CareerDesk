import { createRoot } from "react-dom/client";
import { HashRouter } from "react-router-dom";
import { App } from "./app/App";
import { syncSystemTimeZoneBeforeAppStart } from "./features/settings/systemTimezoneSync";
import { initializeTheme } from "./features/theme/initializeTheme";
import { initializeLocale } from "./i18n/localePreference";
import "./index.css";

initializeTheme();
initializeLocale();

async function start() {
  await syncSystemTimeZoneBeforeAppStart();

  createRoot(document.getElementById("root")!).render(
    <HashRouter>
      <App />
    </HashRouter>,
  );
}

void start();
