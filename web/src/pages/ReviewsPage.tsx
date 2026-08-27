import { useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { keepPreviousData, useQuery } from "@tanstack/react-query";
import { PageHeader } from "@/components/PageHeader";
import { Money } from "@/components/Money";
import { StatusBadge } from "@/components/StatusBadge";
import { EmptyState, ErrorState, SkeletonRows } from "@/components/States";
import { api } from "@/lib/api";
import { absolute, deadlineDistance, relativeFromNow } from "@/lib/time";
import "./ReviewsPage.css";
import { casesPath } from "@/lib/routes";

const TABS = [
  { value: "PENDING", label: "Awaiting decision" },
  { value: "APPROVED", label: "Approved" },
  { value: "REJECTED", label: "Rejected" },
  { value: "EXPIRED", label: "Expired" },
] as const;

/**
 * The review queue.
 *
 * Ordered oldest first, because a review that has been waiting longest is
 * closest to lapsing. The deadline column is the primary urgency signal.
 */
export function ReviewsPage() {
  const [status, setStatus] = useState<string>("PENDING");
  const navigate = useNavigate();

  const reviews = useQuery({
    queryKey: ["reviews", status],
    queryFn: () => api.reviews(status, 100, 0),
    placeholderData: keepPreviousData,
  });

  const rows = reviews.data?.rows ?? [];

  return (
    <>
      <PageHeader
        title="Reviews"
        description="Cases the system declined to act on automatically. A human decides what happens next."
      />

      <div className="page-body">
        <div className="rvq__tabs" role="tablist" aria-label="Review status">
          {TABS.map((tab) => (
            <button
              key={tab.value}
              role="tab"
              type="button"
              aria-selected={status === tab.value}
              className={`rvq__tab${status === tab.value ? " is-active" : ""}`}
              onClick={() => setStatus(tab.value)}
            >
              {tab.label}
              {status === tab.value && reviews.data && (
                <span className="rvq__tab-count">{reviews.data.total}</span>
              )}
            </button>
          ))}
        </div>

        {reviews.isError && (
          <div style={{ marginBottom: "var(--s-4)" }}>
            <ErrorState
              title="Could not load reviews"
              error={reviews.error}
              stale={reviews.data !== undefined}
              onRetry={() => reviews.refetch()}
            />
          </div>
        )}

        <section className="panel" aria-label={`${status} reviews`}>
          {reviews.isPending ? (
            <SkeletonRows rows={5} columns={6} />
          ) : reviews.isError && reviews.data === undefined ? (
            // A failed read is not an empty queue: saying "nothing is waiting"
            // here would tell the reviewer the opposite of what is known.
            <EmptyState
              title="The review queue could not be read"
              description="This is a failure to load, not an empty queue. Retry above."
            />
          ) : rows.length === 0 ? (
            <EmptyState
              title={
                status === "PENDING"
                  ? "Nothing is waiting on a reviewer"
                  : `No ${status.toLowerCase()} reviews`
              }
              description={
                status === "PENDING"
                  ? "Cases appear here when policy refuses to act automatically, or when a payment window closes without payment."
                  : "Decisions of this kind will be listed here once they are made."
              }
              action={<Link className="btn btn--secondary" to={casesPath()}>Browse cases</Link>}
            />
          ) : (
            <div className="table-wrap">
              <table className="ctable rvq__table">
                <caption className="u-visually-hidden">
                  {status} reviews. Activate a row to open the review workspace.
                </caption>
                <colgroup>
                  <col className="col-case" />
                  <col className="col-customer" />
                  <col className="col-amount" />
                  <col className="col-state" />
                  <col className="col-window" />
                  <col className="col-updated" />
                </colgroup>
                <thead>
                  <tr>
                    <th scope="col">Case</th>
                    <th scope="col">Customer</th>
                    <th scope="col" className="is-numeric">Amount</th>
                    <th scope="col">Case state</th>
                    <th scope="col">{status === "PENDING" ? "Decide by" : "Decided"}</th>
                    <th scope="col">Waiting</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => {
                    const expiry = deadlineDistance(row.review_expires_at);
                    const lapsed = row.status === "PENDING" && expiry?.expired;
                    return (
                      <tr
                        key={row.review_id}
                        tabIndex={0}
                        role="link"
                        aria-label={`Review case ${row.case_id}, ${row.case_state}`}
                        className={lapsed ? "is-attention" : undefined}
                        onClick={() => navigate(`/reviews/${row.case_id}`)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter" || e.key === " ") {
                            e.preventDefault();
                            navigate(`/reviews/${row.case_id}`);
                          }
                        }}
                      >
                        <td>
                          <span className="cell-case">
                            <span className="cell-case__id">#{row.case_id}</span>
                            <span className="cell-case__anchor">{row.anchor_key}</span>
                          </span>
                        </td>
                        <td className="cell-customer">{row.customer_ref}</td>
                        <td className="is-numeric">
                          <Money minorUnits={row.amount_minor} currency={row.currency} />
                        </td>
                        <td><StatusBadge state={row.case_state} /></td>
                        <td className="cell-window">
                          {row.status === "PENDING" ? (
                            <span
                              className={lapsed ? "is-urgent" : "is-open"}
                              title={absolute(row.review_expires_at)}
                            >
                              {lapsed ? "deadline passed" : expiry?.text ?? "—"}
                            </span>
                          ) : (
                            <span title={row.decided_at ? absolute(row.decided_at) : undefined}>
                              {row.decided_at ? relativeFromNow(row.decided_at) : "—"}
                              {row.reviewer_ref && (
                                <span className="rvq__by"> by {row.reviewer_ref}</span>
                              )}
                            </span>
                          )}
                        </td>
                        <td className="cell-time" title={absolute(row.created_at)}>
                          {relativeFromNow(row.created_at)}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>
    </>
  );
}
