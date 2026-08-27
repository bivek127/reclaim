/**
 * Typed client for the Reclaim API.
 *
 * Transport only. It does not cache domain meaning, retry writes, or soften a
 * refusal: a 409 from the domain reaches the UI as a 409 so the operator sees
 * what the system actually decided.
 */

import type {
  CaseDetail, CaseHistory, CasePage, Health, Meta, Overview, ReviewQueue, SystemStatus,
} from "./types";

/** An error carrying the status and the server's own explanation. */
export class ApiError extends Error {
  readonly status: number;
  /** True when the case moved under the operator — a reload will show why. */
  readonly isConflict: boolean;
  readonly isUnreachable: boolean;

  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
    this.isConflict = status === 409;
    this.isUnreachable = status === 502 || status === 503 || status === 0;
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let response: Response;
  try {
    response = await fetch(path, {
      ...init,
      headers: { "Content-Type": "application/json", ...(init?.headers ?? {}) },
    });
  } catch (cause) {
    throw new ApiError(
      0,
      "Could not reach the recovery service. Anything shown may be stale.",
    );
  }

  if (response.status === 204) return undefined as T;

  const body = await response.text();
  let parsed: unknown = null;
  try {
    parsed = body ? JSON.parse(body) : null;
  } catch {
    // Fall through: a non-JSON body from a proxy or gateway.
  }

  if (!response.ok) {
    const detail =
      parsed && typeof parsed === "object" && "detail" in parsed
        ? String((parsed as { detail: unknown }).detail)
        : `Request failed (${response.status}).`;
    throw new ApiError(response.status, detail);
  }
  return parsed as T;
}

function qs(params: Record<string, unknown>): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    if (Array.isArray(value)) {
      value.forEach((v) => search.append(key, String(v)));
    } else if (typeof value === "boolean") {
      if (value) search.set(key, "true");
    } else {
      search.set(key, String(value));
    }
  }
  const text = search.toString();
  return text ? `?${text}` : "";
}

export interface CaseQuery {
  state?: string[];
  q?: string;
  needs_attention?: boolean;
  has_pending_review?: boolean;
  sort?: string;
  direction?: string;
  limit?: number;
  offset?: number;
}

export const api = {
  health: () => request<Health>("/api/health"),
  meta: () => request<Meta>("/api/meta"),
  overview: () => request<Overview>("/api/overview"),
  cases: (query: CaseQuery = {}) =>
    request<CasePage>(`/api/cases${qs(query as Record<string, unknown>)}`),
  case: (id: number) => request<CaseDetail>(`/api/cases/${id}`),
  timeline: (id: number) => request<CaseHistory>(`/api/cases/${id}/timeline`),
  reviews: (status = "PENDING", limit = 50, offset = 0) =>
    request<ReviewQueue>(`/api/reviews${qs({ status, limit, offset })}`),
  reviewEvidence: (caseId: number) =>
    request<Record<string, unknown>>(`/api/reviews/${caseId}`),
  system: () => request<SystemStatus>("/api/system"),

  /** Approval proposes an action. The executor still performs the dispatch. */
  approve: (caseId: number, reviewerRef: string, selectedAction: string) =>
    request<Record<string, unknown>>(`/api/reviews/${caseId}/approve`, {
      method: "POST",
      body: JSON.stringify({
        reviewer_ref: reviewerRef,
        selected_action: selectedAction,
      }),
    }),

  reject: (caseId: number, reviewerRef: string) =>
    request<Record<string, unknown>>(`/api/reviews/${caseId}/reject`, {
      method: "POST",
      body: JSON.stringify({ reviewer_ref: reviewerRef }),
    }),
};
