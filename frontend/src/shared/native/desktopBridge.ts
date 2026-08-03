export type CareerDeskDesktopApi = {
  select_data_directory?: () => Promise<string | null>;
  download_job_import_template?: (locale: "zh-CN" | "en") => Promise<string>;
  open_job_import_template?: (path: string) => Promise<boolean>;
};

declare global {
  interface Window {
    pywebview?: {
      api?: CareerDeskDesktopApi;
    };
  }
}

export function desktopApi(): CareerDeskDesktopApi | undefined {
  return window.pywebview?.api;
}
