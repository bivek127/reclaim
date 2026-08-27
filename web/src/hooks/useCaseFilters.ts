import { useCallback, useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import type { CaseQuery } from "@/lib/api";

/**
 * Queue filters held in the URL.
 *
 * The URL is the single source of truth, so a filtered view is shareable,
 * bookmarkable, and survives reload and back-navigation — an operator can send
 * a colleague exactly what they are looking at.
 *
 * Only parameters the API already supports are represented here. The page
 * never filters or sorts client-side, because the server owns the result set.
 */

export const SORT_OPTIONS = [
  { value: "updated_at", label: "Last updated" },
  { value: "created_at", label: "Opened" },
  { value: "amount", label: "Amount" },
] as const;

export type SortValue = (typeof SORT_OPTIONS)[number]["value"];

const SORT_VALUES = SORT_OPTIONS.map((o) => o.value) as readonly string[];
const PAGE_SIZE = 25;

export interface CaseFilters {
  q: string;
  states: string[];
  needsAttention: boolean;
  pendingReview: boolean;
  sort: SortValue;
  direction: "asc" | "desc";
  page: number;
  pageSize: number;
}

export function useCaseFilters() {
  const [params, setParams] = useSearchParams();

  const filters: CaseFilters = useMemo(() => {
    const rawSort = params.get("sort") ?? "updated_at";
    const rawPage = Number(params.get("page") ?? "1");
    return {
      q: params.get("q") ?? "",
      states: params.getAll("state"),
      needsAttention: params.get("attention") === "1",
      pendingReview: params.get("review") === "1",
      sort: (SORT_VALUES.includes(rawSort) ? rawSort : "updated_at") as SortValue,
      direction: params.get("dir") === "asc" ? "asc" : "desc",
      page: Number.isFinite(rawPage) && rawPage > 0 ? Math.floor(rawPage) : 1,
      pageSize: PAGE_SIZE,
    };
  }, [params]);

  /** Any change other than paging returns to page one. */
  const update = useCallback(
    (patch: Partial<CaseFilters>) => {
      const next = new URLSearchParams();
      const merged = { ...filters, ...patch };
      const resetPage = !("page" in patch);

      if (merged.q.trim()) next.set("q", merged.q.trim());
      merged.states.forEach((s) => next.append("state", s));
      if (merged.needsAttention) next.set("attention", "1");
      if (merged.pendingReview) next.set("review", "1");
      if (merged.sort !== "updated_at") next.set("sort", merged.sort);
      if (merged.direction !== "desc") next.set("dir", merged.direction);
      const page = resetPage ? 1 : merged.page;
      if (page > 1) next.set("page", String(page));

      setParams(next, { replace: true });
    },
    [filters, setParams],
  );

  const clear = useCallback(() => setParams(new URLSearchParams(), { replace: true }), [setParams]);

  const toggleState = useCallback(
    (state: string) => {
      const has = filters.states.includes(state);
      update({
        states: has ? filters.states.filter((s) => s !== state) : [...filters.states, state],
      });
    },
    [filters.states, update],
  );

  /** Clicking the active sort column flips direction; a new column starts descending. */
  const toggleSort = useCallback(
    (sort: SortValue) => {
      if (filters.sort === sort) {
        update({ direction: filters.direction === "desc" ? "asc" : "desc" });
      } else {
        update({ sort, direction: "desc" });
      }
    },
    [filters.sort, filters.direction, update],
  );

  const query: CaseQuery = useMemo(
    () => ({
      q: filters.q.trim() || undefined,
      state: filters.states.length ? filters.states : undefined,
      needs_attention: filters.needsAttention || undefined,
      has_pending_review: filters.pendingReview || undefined,
      sort: filters.sort,
      direction: filters.direction,
      limit: filters.pageSize,
      offset: (filters.page - 1) * filters.pageSize,
    }),
    [filters],
  );

  const activeCount =
    (filters.q.trim() ? 1 : 0) +
    filters.states.length +
    (filters.needsAttention ? 1 : 0) +
    (filters.pendingReview ? 1 : 0);

  return { filters, query, update, clear, toggleState, toggleSort, activeCount };
}
