/**
 * Internal link targets, built in one place.
 *
 * Queue filters live in the URL, so a hand-written query string is a silent
 * failure: an unrecognised parameter is ignored and the operator gets an
 * unfiltered list that looks like a filtered one. Every link is built here so
 * the parameter names have exactly one definition, shared with the hook that
 * reads them back.
 */

/** Query-parameter names the cases queue understands. */
export const CASE_PARAM = {
  query: "q",
  state: "state",
  attention: "attention",
  review: "review",
  sort: "sort",
  direction: "dir",
  page: "page",
} as const;

export interface CaseLinkFilters {
  query?: string;
  states?: string[];
  attention?: boolean;
  pendingReview?: boolean;
}

export function casesPath(filters: CaseLinkFilters = {}): string {
  const params = new URLSearchParams();
  if (filters.query?.trim()) params.set(CASE_PARAM.query, filters.query.trim());
  filters.states?.forEach((s) => params.append(CASE_PARAM.state, s));
  if (filters.attention) params.set(CASE_PARAM.attention, "1");
  if (filters.pendingReview) params.set(CASE_PARAM.review, "1");
  const search = params.toString();
  return search ? `/cases?${search}` : "/cases";
}

export const casePath = (caseId: number) => `/cases/${caseId}`;
export const caseTimelinePath = (caseId: number) => `/cases/${caseId}/timeline`;
export const reviewPath = (caseId: number) => `/reviews/${caseId}`;
export const reviewsPath = () => "/reviews";
export const overviewPath = () => "/";
export const systemPath = () => "/system";
