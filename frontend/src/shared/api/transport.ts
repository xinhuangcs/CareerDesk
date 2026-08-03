import { WRITE_HEADERS } from "./headers.ts";
import { getRuntimeLocale } from "./runtimeLocale.ts";

export class HttpError extends Error {
  readonly status: number;
  readonly requestId?: string;
  readonly code?: string;
  readonly problemType?: string;
  readonly errors?: readonly unknown[];
  readonly params?: Readonly<Record<string, unknown>>;

  constructor(
    status: number,
    message: string,
    options: {
      requestId?: string;
      code?: string;
      problemType?: string;
      errors?: readonly unknown[];
      params?: Readonly<Record<string, unknown>>;
    } = {},
  ) {
    super(message);
    this.name = "HttpError";
    this.status = status;
    this.requestId = options.requestId;
    this.code = options.code;
    this.problemType = options.problemType;
    this.errors = options.errors;
    this.params = options.params;
  }
}

const ERROR_COPY: Readonly<Record<string, readonly [string, string]>> = {
  database_busy: ["后台任务正在写入，本次修改未保存；请稍后重试。", "A background task is writing data. Your change was not saved; try again shortly."],
  strict_offline: ["当前处于严格离线模式，已阻止外部访问。", "Strict offline mode blocked external access."],
  model_not_configured: ["模型尚未配置，请在设置中完成模型与凭证配置。", "No model is configured. Complete model and credential setup in Settings."],
  model_capabilities_missing: ["模型能力信息不完整，请在设置中补齐后重试。", "Model capability information is incomplete. Complete it in Settings and retry."],
  request_validation: ["请求内容不完整或格式不正确，请检查后重试。", "Some fields are missing or invalid. Check the form and try again."],
  internal_error: ["服务器处理请求时发生错误，请稍后重试。", "The server could not process the request. Try again later."],
  http_400: ["请求内容无法处理，请检查后重试。", "The request could not be processed. Check it and try again."],
  http_401: ["登录状态已失效，请重新登录。", "Your session has expired. Sign in again."],
  http_403: ["当前账户无权执行此操作。", "Your account is not allowed to perform this action."],
  http_404: ["请求的内容已不存在或无法找到。", "The requested item no longer exists or could not be found."],
  http_409: ["内容已在其他位置发生变化，请刷新后重试。", "This item changed elsewhere. Refresh and try again."],
  http_413: ["提交的内容过大，请缩小文件或内容后重试。", "The submitted content is too large. Reduce its size and try again."],
  http_415: ["不支持这种文件或内容格式。", "This file or content format is not supported."],
  http_422: ["请求内容不完整或格式不正确，请检查后重试。", "Some fields are missing or invalid. Check the form and try again."],
  http_429: ["请求过于频繁，请稍后再试。", "Too many requests. Try again shortly."],
  http_500: ["服务器处理请求时发生错误，请稍后重试。", "The server could not process the request. Try again later."],
  http_502: ["服务暂时无法响应，请稍后重试。", "The service is temporarily unavailable. Try again later."],
  http_503: ["服务暂时不可用，请稍后重试。", "The service is temporarily unavailable. Try again later."],
  http_504: ["服务响应超时，请稍后重试。", "The service timed out. Try again later."],
};

function localizedHttpMessage(status: number, code?: string): string {
  const locale = getRuntimeLocale();
  const pair = (code ? ERROR_COPY[code] : undefined) ?? ERROR_COPY[`http_${status}`];
  if (pair) return locale === "en" ? pair[1] : pair[0];
  return locale === "en" ? `Request did not complete (HTTP ${status}).` : `请求未完成（HTTP ${status}）。`;
}

async function responseError(response: Response): Promise<HttpError> {
  const payload = await response.json().catch(() => null) as Record<string, unknown> | null;
  const detail = payload?.detail;
  const errors = Array.isArray(payload?.errors) ? payload.errors : undefined;
  const code = typeof payload?.code === "string" ? payload.code : `http_${response.status}`;
  const params = payload?.params !== null && typeof payload?.params === "object" && !Array.isArray(payload.params)
    ? payload.params as Record<string, unknown>
    : undefined;
  const options = {
    requestId: response.headers.get("X-Request-ID")
      ?? (typeof payload?.request_id === "string" ? payload.request_id : undefined),
    code,
    problemType: typeof payload?.type === "string" ? payload.type : undefined,
    errors,
    params,
  };
  // Backend detail and validator messages are diagnostic data, not localized UI copy.
  void detail;
  return new HttpError(response.status, localizedHttpMessage(response.status, code), options);
}

async function ensureOk(response: Response): Promise<void> {
  if (!response.ok) throw await responseError(response);
}

export async function getJson<T>(url: string, init?: RequestInit): Promise<T> {
  const response = await fetch(url, init);
  await ensureOk(response);
  return response.json() as Promise<T>;
}

export async function postJson<T>(
  url: string,
  body: unknown = {},
  init?: Pick<RequestInit, "signal">,
): Promise<T> {
  const response = await fetch(url, {
    method: "POST",
    headers: { ...WRITE_HEADERS, "Content-Type": "application/json" },
    body: JSON.stringify(body),
    signal: init?.signal,
  });
  await ensureOk(response);
  return response.json() as Promise<T>;
}

// Leave multipart Content-Type unset so the browser can append the boundary.
export async function postForm<T>(
  url: string,
  form: FormData,
  init?: Pick<RequestInit, "signal">,
): Promise<T> {
  const response = await fetch(url, {
    method: "POST",
    headers: WRITE_HEADERS,
    body: form,
    signal: init?.signal,
  });
  await ensureOk(response);
  return response.json() as Promise<T>;
}

export async function del<T>(url: string): Promise<T> {
  const response = await fetch(url, { method: "DELETE", headers: WRITE_HEADERS });
  await ensureOk(response);
  return response.json() as Promise<T>;
}

export async function putJson<T>(url: string, body: unknown): Promise<T> {
  const response = await fetch(url, {
    method: "PUT",
    headers: { ...WRITE_HEADERS, "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  await ensureOk(response);
  return response.json() as Promise<T>;
}

export async function putForm<T>(url: string, form: FormData): Promise<T> {
  const response = await fetch(url, { method: "PUT", headers: WRITE_HEADERS, body: form });
  await ensureOk(response);
  return response.json() as Promise<T>;
}
