import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { PageHeader } from "@/components/PageHeader";
import { Section } from "@/components/Section";
import { Money } from "@/components/Money";
import { StatusBadge } from "@/components/StatusBadge";
import { EmptyState, ErrorState, SkeletonBlock, SkeletonRows } from "@/components/States";
import { MetricBand } from "@/components/overview/MetricBand";
import { AttentionList } from "@/components/overview/AttentionList";
import { StateDistribution } from "@/components/overview/StateDistribution";
import { SystemStatus } from "@/components/overview/SystemStatus";
import { RecentActivity } from "@/components/overview/RecentActivity";
import { api, ApiError } from "@/lib/api";
import { formatMoney } from "@/lib/money";
import { deadlineDistance, relativeFromNow } from "@/lib/time";
import "./OverviewPage.css";
import { casesPath, reviewPath, reviewsPath } from "@/lib/routes";
import type { Overview } from "@/lib/types";

// Grouped by currency at the source, so nothing here sums across currencies —
// an undenominated total would misrepresent money the read model never merged.
function recoveredNote(o: Overview): string {
  if (o.recovered_by_currency.length === 0) {
    return "Cases with payment independently verified by two sources.";
  }
  const amounts = o.recovered_by_currency
    .map((r) => formatMoney(r.amount_minor, r.currency))
    .join(", ");
  return `${amounts} recovered, independently verified by two sources.`;
}

/**
 * The operations landing screen.
 *
 * Reads: is anything wrong, what needs a human, what shape is the estate in,
 * what just changed. Every figure comes from the API — there are no trends,
 * rates, or comparisons here because the backend stores no history to derive
 * them from, and a decorative chart would be a claim the data cannot support.
 */
export function OverviewPage() {
  const overview = useQuery({ queryKey: ["overview"], queryFn: api.overview, refetchInterval: 30_000 });
  const meta = useQuery({ queryKey: ["meta"], queryFn: api.meta, staleTime: 5 * 60 * 1000 });
  const system = useQuery({ queryKey: ["system"], queryFn: api.system, refetchInterval: 60_000 });

  const attention = useQuery({
    queryKey: ["cases", "attention-preview"],
    queryFn: () => api.cases({ needs_attention: true, sort: "amount", direction: "desc", limit: 5 }),
    refetchInterval: 30_000,
  });

  const reviews = useQuery({
    queryKey: ["reviews", "PENDING", "overview"],
    queryFn: () => api.reviews("PENDING", 4, 0),
    refetchInterval: 30_000,
  });

  const unreachable =
    overview.isError && overview.error instanceof ApiError && overview.error.isUnreachable;

  if (overview.isPending) {
    return (
      <>
        <PageHeader title="Overview" description="Loading operational state…" />
        <div className="page-body">
          <SkeletonBlock height={104} />
          <div style={{ height: "var(--s-5)" }} />
          <div className="ov-grid">
            <SkeletonBlock height={320} />
            <SkeletonBlock height={320} />
          </div>
        </div>
      </>
    );
  }

  if (overview.isError) {
    return (
      <>
        <PageHeader title="Overview" />
        <div className="page-body">
          {/* An unreachable service must never read as "nothing is happening". */}
          <ErrorState
            title="Operational state is unavailable"
            error={overview.error}
            onRetry={() => overview.refetch()}
          />
          <p className="ov-unavailable">
            No figures are shown because none could be read. This is not the same as
            an empty system.
          </p>
        </div>
      </>
    );
  }

  const o = overview.data;
  const oldestReview = o.oldest_pending_review_at;

  return (
    <>
      <PageHeader
        title="Overview"
        description="What needs a human right now, and what the recovery estate is doing."
      />

      <div className="page-body">
        <MetricBand
          metrics={[
            {
              label: "Needs attention",
              value: o.attention_total,
              note: "Escalated, ambiguous, or halted — a human decides what happens.",
              to: "/cases?attention=1",
              tone: o.attention_total > 0 ? "attention" : "neutral",
            },
            {
              label: "Awaiting review",
              value: o.pending_reviews,
              note: oldestReview
                ? `Oldest waiting ${relativeFromNow(oldestReview)}.`
                : "No case is waiting on a reviewer.",
              to: "/reviews",
              tone: o.pending_reviews > 0 ? "attention" : "neutral",
            },
            {
              label: "In flight",
              value: o.in_flight_total,
              note: "Executing, reconciling, or waiting on the customer. No action needed.",
              to: "/cases?state=EXECUTING&state=RECONCILING&state=AWAITING_CUSTOMER",
            },
            {
              label: "Recovered",
              value: o.recovered_count,
              note: recoveredNote(o),
              to: "/cases?state=VERIFIED_RECOVERED",
              tone: o.recovered_count > 0 ? "success" : "neutral",
            },
          ]}
        />

        <div className="ov-grid">
          <div className="ov-grid__main">
            <Section
              title="Needs attention"
              note="Highest value first. These will not resolve without a human."
              aside={<Link className="btn btn--sm" to={casesPath({ attention: true })}>All cases</Link>}
            >
              {attention.isPending ? (
                <SkeletonRows rows={4} columns={3} />
              ) : attention.isError ? (
                <ErrorState error={attention.error} onRetry={() => attention.refetch()} />
              ) : (
                <AttentionList
                  rows={attention.data?.rows ?? []}
                  total={attention.data?.total ?? 0}
                />
              )}
            </Section>

            <Section
              title="Awaiting human review"
              note="Approving proposes an action; the executor dispatches it."
              aside={<Link className="btn btn--sm" to={reviewsPath()}>Review queue</Link>}
            >
              {reviews.isPending ? (
                <SkeletonRows rows={3} columns={3} />
              ) : reviews.isError ? (
                <ErrorState error={reviews.error} onRetry={() => reviews.refetch()} />
              ) : (reviews.data?.rows.length ?? 0) === 0 ? (
                <EmptyState
                  title="No cases are waiting on a reviewer"
                  description="Cases arrive here when policy refuses to act automatically, or when a payment window closes without payment. An empty queue is a normal, healthy state."
                />
              ) : (
                <ul className="ov-reviews">
                  {reviews.data?.rows.map((row) => {
                    const expiry = deadlineDistance(row.review_expires_at);
                    const lapsed = expiry?.expired === true;
                    return (
                      <li key={row.review_id}>
                        <Link className="ov-review" to={reviewPath(row.case_id)}>
                          <span className="ov-review__id">#{row.case_id}</span>
                          <span className="ov-review__cust u-mono">{row.customer_ref}</span>
                          <StatusBadge state={row.case_state} />
                          <Money minorUnits={row.amount_minor} currency={row.currency} />
                          <span className={`ov-review__due${lapsed ? " is-lapsed" : ""}`}>
                            {lapsed ? "deadline passed" : `decide ${expiry?.text ?? ""}`}
                          </span>
                        </Link>
                      </li>
                    );
                  })}
                </ul>
              )}
            </Section>
  <Section title="Recent activity" note="The latest recorded events across all cases.">
              <RecentActivity events={o.recent_activity} />
            </Section>
          </div>

          <aside className="ov-grid__side" aria-label="System and estate">
            <Section title="System" note="Only what the recovery service reports.">
              <SystemStatus
                overview={o}
                system={system.data}
                environment={meta.data?.environment}
                unreachable={unreachable}
              />
            </Section>

            <Section title="Recovery states" note="Every case, by the state it is in now.">
              <StateDistribution
                counts={o.state_counts}
                order={meta.data?.case_states ?? Object.keys(o.state_counts)}
              />
            </Section>

          </aside>
        </div>
      </div>
    </>
  );
}
