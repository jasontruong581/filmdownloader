/**
 * Typed fetch wrapper.
 *
 * Types come from the backend's OpenAPI schema, so a model change breaks the
 * build rather than the runtime. API refusals carry a machine-readable reason;
 * they are turned into an error that keeps it, so views can explain what
 * happened instead of showing a generic failure.
 */
import type { components } from "./types.gen";

type Schemas = components["schemas"];

export type Health = Schemas["HealthOut"];
export type Format = Schemas["FormatOut"];
export type Resolved = Schemas["ResolveOut"];
export type BatchProbe = Schemas["BatchProbeOut"];
export type BatchItem = Schemas["BatchItemOut"];
export type BatchVerify = Schemas["BatchVerifyOut"];
export type Batch = Schemas["BatchOut"];
export type Job = Schemas["JobOut"];
export type LibraryItem = Schemas["LibraryItemOut"];
export type Settings = Schemas["SettingsOut"];
export type SettingsPatch = Schemas["SettingsIn"];

/** Job statuses, mirroring the backend enum. */
export const TERMINAL_STATUSES = ["completed", "failed", "cancelled", "interrupted"] as const;

export function isTerminal(status: string): boolean {
  return (TERMINAL_STATUSES as readonly string[]).includes(status);
}

const TOKEN_KEY = "filmdownloader.token";

export function storedToken(): string {
  try {
    return window.localStorage.getItem(TOKEN_KEY) ?? "";
  } catch {
    return "";
  }
}

export function storeToken(value: string): void {
  try {
    if (value) window.localStorage.setItem(TOKEN_KEY, value);
    else window.localStorage.removeItem(TOKEN_KEY);
  } catch {
    /* private windows and blocked site data are not an error here */
  }
}

export class ApiError extends Error {
  readonly status: number;
  readonly reason: string;

  constructor(status: number, reason: string, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.reason = reason;
  }
}

type Detail = { reason?: string; message?: string };

async function readError(response: Response): Promise<ApiError> {
  let reason = "request_failed";
  let message = `${response.status} ${response.statusText}`;
  try {
    const body = (await response.json()) as { detail?: Detail | string };
    const detail = body?.detail;
    if (typeof detail === "string") {
      message = detail;
    } else if (detail && typeof detail === "object") {
      reason = detail.reason ?? reason;
      message = detail.message ?? message;
    }
  } catch {
    /* a non-JSON body leaves the status line as the message */
  }
  return new ApiError(response.status, reason, message);
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const token = storedToken();
  const headers = new Headers(init.headers);
  headers.set("Accept", "application/json");
  if (init.body) headers.set("Content-Type", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);

  const response = await fetch(path, { ...init, headers });
  if (!response.ok) throw await readError(response);
  if (response.status === 204) return undefined as T;
  return (await response.json()) as T;
}

const post = <T,>(path: string, body: unknown) =>
  request<T>(path, { method: "POST", body: JSON.stringify(body) });

export const api = {
  health: () => request<Health>("/api/health"),

  resolve: (url: string, engines?: string[]) =>
    post<Resolved>("/api/resolve", { url, engines: engines ?? null }),

  probeBatch: (url: string) => post<BatchProbe>("/api/batch/probe", { url }),

  verifyBatch: (items: BatchItem[], count = 2) =>
    post<BatchVerify>("/api/batch/verify", { items, count }),

  queueBatch: (payload: {
    items: BatchItem[];
    source_url?: string;
    capability?: string;
    confidence?: string;
  }) => post<Batch>("/api/batch/jobs", payload),

  batch: (id: string) => request<Batch>(`/api/batches/${encodeURIComponent(id)}`),

  queueJob: (payload: {
    url?: string;
    resolution_id?: string;
    format_id?: string;
    title?: string;
  }) => post<Job>("/api/jobs", payload),

  jobs: (status?: string) =>
    request<Job[]>(`/api/jobs${status ? `?status=${encodeURIComponent(status)}` : ""}`),

  cancelJob: (id: string) =>
    request<Job>(`/api/jobs/${encodeURIComponent(id)}`, { method: "DELETE" }),

  retryJob: (id: string) =>
    request<Job>(`/api/jobs/${encodeURIComponent(id)}/retry`, { method: "POST" }),

  library: () => request<LibraryItem[]>("/api/library"),

  fileUrl: (id: string) => `/api/library/${id.split("/").map(encodeURIComponent).join("/")}/file`,

  settings: () => request<Settings>("/api/settings"),

  saveSettings: (patch: SettingsPatch) =>
    request<Settings>("/api/settings", { method: "PUT", body: JSON.stringify(patch) }),
};
