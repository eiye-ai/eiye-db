import type {
  AccessReview,
  DataSource,
  DataSourceType,
  Metric,
  Policy,
  QueryResponse,
  Relationship,
  Schema,
} from "./types";

// Default to the Vite proxy path (/api/v1). Set VITE_API_BASE to hit the backend
// directly (e.g. http://localhost:8000/api/v1) — the backend allows CORS for it.
const BASE = import.meta.env.VITE_API_BASE ?? "/api/v1";

const API_KEY_STORAGE = "eiye_api_key";

export function getApiKey(): string {
  return localStorage.getItem(API_KEY_STORAGE) ?? "";
}

export function setApiKey(key: string): void {
  if (key) localStorage.setItem(API_KEY_STORAGE, key);
  else localStorage.removeItem(API_KEY_STORAGE);
}

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.status = status;
  }
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...((init?.headers as Record<string, string>) ?? {}),
  };
  const key = getApiKey();
  if (key) headers["X-API-Key"] = key;

  const res = await fetch(BASE + path, { ...init, headers });
  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      detail = body.detail ?? detail;
    } catch {
      /* non-JSON error body */
    }
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export interface CreateBody {
  name: string;
  type: DataSourceType;
  config: Record<string, unknown>;
}

export interface UpdateBody {
  name?: string;
  config?: Record<string, unknown>;
}

export interface QueryBody {
  datasource_id: string;
  request: Record<string, unknown>;
  limit: number;
}

export interface PolicyBody {
  name: string;
  description: string;
  effect: "allow" | "deny";
  resource_id: string;
  actions: string[];
  subjects: string[];
}

export const api = {
  list: () => req<DataSource[]>("/datasources"),
  get: (id: string) => req<DataSource>(`/datasources/${id}`),
  create: (body: CreateBody) => req<DataSource>("/datasources", { method: "POST", body: JSON.stringify(body) }),
  update: (id: string, body: UpdateBody) =>
    req<DataSource>(`/datasources/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  remove: (id: string) => req<void>(`/datasources/${id}`, { method: "DELETE" }),
  test: (id: string) => req<DataSource>(`/datasources/${id}/test`, { method: "POST" }),
  discover: (id: string) => req<Schema>(`/datasources/${id}/discover`, { method: "POST" }),
  schema: (id: string) => req<Schema>(`/surface/schema/${id}`),
  query: (body: QueryBody) => req<QueryResponse>("/query", { method: "POST", body: JSON.stringify(body) }),

  relationships: (status?: string) =>
    req<Relationship[]>(`/semantic/relationships${status ? `?status=${status}` : ""}`),
  detect: () => req<Relationship[]>("/semantic/detect", { method: "POST" }),
  reviewRelationship: (id: string, status: "approved" | "rejected") =>
    req<Relationship>(`/semantic/relationships/${id}`, { method: "PUT", body: JSON.stringify({ status }) }),
  metrics: () => req<Metric[]>("/semantic/metrics"),
  reviewMetric: (id: string, status: "approved" | "rejected") =>
    req<Metric>(`/semantic/metrics/${id}/review`, { method: "PUT", body: JSON.stringify({ status }) }),
  removeMetric: (id: string) => req<void>(`/semantic/metrics/${id}`, { method: "DELETE" }),

  // Access control. All three are admin-only: a policy list maps out what is
  // being protected. A key id is caller-supplied text, so it is encoded.
  access: (keyId: string) => req<AccessReview>(`/access/${encodeURIComponent(keyId)}`),
  policies: () => req<Policy[]>("/policies"),
  createPolicy: (body: PolicyBody) => req<Policy>("/policies", { method: "POST", body: JSON.stringify(body) }),
  removePolicy: (id: string) => req<void>(`/policies/${id}`, { method: "DELETE" }),
};
