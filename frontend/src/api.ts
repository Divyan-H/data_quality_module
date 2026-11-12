import type { AgentState, CandidateTable, ColumnChoiceTable, EventItem, IssuesView, LlmStatus, PhaseKey, Results, RowsPage, Run, RunListItem, ServiceOptions } from "./types";

const BASE = "/api/v1";
export const AUTH_EVENT = "dq-auth-required";

export interface SessionState {
  auth_required: boolean;
  signed_in: boolean;
}

export class ApiError extends Error {
  constructor(
    public status: number,
    public code: string,
    message: string,
    public extra: Record<string, unknown> = {},
  ) {
    super(message);
  }
}

async function parse<T>(res: Response): Promise<T> {
  if (res.ok) {
    return res.status === 204 ? (undefined as T) : ((await res.json()) as T);
  }
  let body: { error?: { code?: string; message?: string; [k: string]: unknown }; detail?: unknown } | null = null;
  try {
    body = await res.json();
  } catch {
    /* not JSON */
  }
  const err = body?.error;
  if (err) {
    const { code, message, ...extra } = err;
    // The service asks for its access token: the app shows the sign-in screen (App listens for this event).
    if (res.status === 401 && code === "auth_required") window.dispatchEvent(new Event(AUTH_EVENT));
    throw new ApiError(res.status, code ?? "error", message ?? res.statusText, extra);
  }
  if (res.status === 422 && Array.isArray(body?.detail)) {
    throw new ApiError(422, "bad_request", "The request was not valid.");
  }
  throw new ApiError(res.status, "http_error", `The service answered ${res.status} ${res.statusText}.`);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(BASE + path, init);
  } catch {
    throw new ApiError(0, "unreachable", "Cannot reach the Data Pipeline service. Is it running?");
  }
  return parse<T>(res);
}

const json = (body: unknown): RequestInit => ({
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify(body),
});

export interface StartOptions {
  llm?: "on" | "off";
  llm_review?: "near_ties" | "all";
  fixes?: Record<string, boolean>;
  /** Phase B: {table ref: columns where zero is a valid value}; D-27 never fills these. */
  zero_expected?: Record<string, string[]>;
  restart?: boolean;
}

export const api = {
  session: () => request<SessionState>("/session"),
  signIn: (token: string) => request<SessionState>("/session", json({ token })),
  signOut: () => request<SessionState>("/session", { method: "DELETE" }),
  options: () => request<ServiceOptions>("/options"),
  llmStatus: (fresh = false) => request<LlmStatus>(`/llm/status${fresh ? "?fresh=true" : ""}`),
  listRuns: () => request<RunListItem[]>("/runs"),
  getRun: (id: string) => request<Run>(`/runs/${id}`),
  deleteRun: (id: string) => request<void>(`/runs/${id}`, { method: "DELETE" }),

  createRun(files: { file: File; path: string }[], name: string, llm: "on" | "off" = "on") {
    const form = new FormData();
    files.forEach((f) => {
      form.append("files", f.file, f.file.name);
      form.append("paths", f.path);
    });
    form.append("name", name);
    form.append("llm", llm);
    return request<Run>("/runs", { method: "POST", body: form });
  },

  start: (id: string, key: PhaseKey, opts: StartOptions = {}) => request<Run>(`/runs/${id}/phases/${key}/start`, json(opts)),
  cancel: (id: string) => request<Run>(`/runs/${id}/cancel`, { method: "POST" }),
  skip: (id: string, key: PhaseKey, reason?: string) => request<Run>(`/runs/${id}/phases/${key}/skip`, json({ reason })),
  accept: (id: string, key: PhaseKey) => request<Run>(`/runs/${id}/phases/${key}/accept`, { method: "POST" }),
  selectTables: (id: string, tables: string[]) => request<Run>(`/runs/${id}/phases/A/selection`, json({ tables })),
  summary: <T>(id: string, key: PhaseKey) => request<T>(`/runs/${id}/phases/${key}/summary`),

  uploadReview(id: string, key: PhaseKey, file: File, llm?: "off") {
    const form = new FormData();
    form.append("file", file, file.name);
    if (llm) form.append("llm", llm);
    return request<Run>(`/runs/${id}/phases/${key}/review`, { method: "POST", body: form });
  },

  columns: (id: string, key: PhaseKey) => request<{ tables: ColumnChoiceTable[] }>(`/runs/${id}/phases/${key}/columns`),
  tables: (id: string, key: PhaseKey) => request<{ tables: CandidateTable[] }>(`/runs/${id}/phases/${key}/tables`),
  uploadConfig(id: string, key: PhaseKey, file: File, table: string) {
    const form = new FormData();
    form.append("file", file, file.name);
    form.append("table", table);
    return request<Run>(`/runs/${id}/phases/${key}/config`, { method: "POST", body: form });
  },
  recheckConfig: (id: string, key: PhaseKey, table?: string) =>
    request<Run>(`/runs/${id}/phases/${key}/config/recheck${table ? `?table=${encodeURIComponent(table)}` : ""}`, { method: "POST" }),

  // Fix-with-agent: plan + preview (waits), apply / undo (jobs; the outcome arrives through the run), reads.
  agent: (id: string, key: PhaseKey) => request<AgentState>(`/runs/${id}/phases/${key}/agent`),
  agentSay: (id: string, key: PhaseKey, text: string) =>
    request<{ reply: unknown; state: AgentState }>(`/runs/${id}/phases/${key}/agent/messages`, json({ text })),
  agentApply: (id: string, key: PhaseKey, cs: string, confirm = false) =>
    request<Run>(`/runs/${id}/phases/${key}/agent/changesets/${encodeURIComponent(cs)}/apply`, json({ confirm })),
  agentDiscard: (id: string, key: PhaseKey, cs: string) =>
    request<AgentState>(`/runs/${id}/phases/${key}/agent/changesets/${encodeURIComponent(cs)}/discard`, { method: "POST" }),
  agentUndo: (id: string, key: PhaseKey) => request<Run>(`/runs/${id}/phases/${key}/agent/undo`, { method: "POST" }),
  agentIssues: (id: string, key: PhaseKey) => request<IssuesView>(`/runs/${id}/phases/${key}/agent/issues`),
  agentRows(id: string, key: PhaseKey, q: { table: string; offset?: number; limit?: number; rule?: string; column?: string; only?: "changed" | "flagged" }) {
    const p = new URLSearchParams();
    Object.entries(q).forEach(([k, v]) => v !== undefined && v !== null && v !== "" && p.set(k, String(v)));
    return request<RowsPage>(`/runs/${id}/phases/${key}/agent/rows?${p.toString()}`);
  },

  events: (id: string, after: number) => request<{ events: EventItem[]; cursor: number }>(`/runs/${id}/events?after=${after}`),
  results: (id: string) => request<Results>(`/runs/${id}/results`),

  // Downloads are plain links so the browser streams them to disk.
  fileUrl: (id: string, artifact: string) => `${BASE}/runs/${id}/files/${encodeURIComponent(artifact)}`,
  bundleUrl: (id: string) => `${BASE}/runs/${id}/bundle`,
  templateUrl: (id: string, key: PhaseKey, table?: string) =>
    `${BASE}/runs/${id}/phases/${key}/config-template${table ? `?table=${encodeURIComponent(table)}` : ""}`,
};
