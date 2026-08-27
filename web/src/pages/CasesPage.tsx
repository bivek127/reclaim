import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { PageHeader } from "@/components/PageHeader";
import { FilterBar } from "@/components/FilterBar";
import { CasesTable } from "@/components/CasesTable";
import { Pagination } from "@/components/Pagination";
import { EmptyState, ErrorState, SkeletonRows } from "@/components/States";
import { useCaseFilters } from "@/hooks/useCaseFilters";
import { api } from "@/lib/api";

/**
 * The case queue: the operator's working surface for finding and triaging.
 *
 * Filtering, sorting, and paging all happen server-side — the page renders
 * whatever the read model returns and never re-orders or re-filters a result
 * set locally, which would make the visible list disagree with the count.
 */
export function CasesPage() {
  const { filters, query, update, clear, toggleState, toggleSort, activeCount } =
    useCaseFilters();

  const meta = useQuery({ queryKey: ["meta"], queryFn: api.meta, staleTime: 5 * 60 * 1000 });

  const cases = useQuery({
    queryKey: ["cases", query],
    queryFn: () => api.cases(query),
    // Keep the previous page visible while the next one loads, so the table
    // does not collapse to a skeleton on every filter change.
    placeholderData: keepPreviousData,
  });

  const rows = cases.data?.rows ?? [];
  const total = cases.data?.total;
  const showingStale = cases.isError && cases.data !== undefined;

  return (
    <>
      <PageHeader
        title="Cases"
        description="Every recovery case, newest activity first. Rows marked on the left need a human."
      />

      <div className="page-body">
        <FilterBar
          filters={filters}
          availableStates={meta.data?.case_states ?? []}
          attentionStates={meta.data?.attention_states ?? []}
          activeCount={activeCount}
          resultTotal={total}
          onSearch={(q) => update({ q })}
          onToggleState={toggleState}
          onToggleAttention={() => update({ needsAttention: !filters.needsAttention })}
          onTogglePendingReview={() => update({ pendingReview: !filters.pendingReview })}
          onClear={clear}
        />

        {cases.isError && (
          <div style={{ marginBottom: "var(--s-4)" }}>
            <ErrorState
              title="Could not load cases"
              error={cases.error}
              stale={showingStale}
              onRetry={() => cases.refetch()}
            />
          </div>
        )}

        <section className="panel" aria-label="Recovery cases">
          {cases.isPending ? (
            <SkeletonRows rows={8} columns={7} />
          ) : rows.length === 0 ? (
            <EmptyState
              title={activeCount > 0 ? "No cases match these filters" : "No cases yet"}
              description={
                activeCount > 0
                  ? "Try removing a filter or widening the search."
                  : "Cases appear here once a failed payment has been ingested."
              }
              action={
                activeCount > 0 ? (
                  <button type="button" className="btn btn--secondary" onClick={clear}>
                    Clear filters
                  </button>
                ) : undefined
              }
            />
          ) : (
            <>
              <CasesTable
                rows={rows}
                sort={filters.sort}
                direction={filters.direction}
                onSort={toggleSort}
              />
              <Pagination
                page={filters.page}
                pageSize={filters.pageSize}
                total={total ?? 0}
                onPage={(page) => update({ page })}
              />
            </>
          )}
        </section>
      </div>
    </>
  );
}
