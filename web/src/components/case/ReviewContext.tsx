import { Link } from "react-router-dom";
import { absolute, deadlineDistance, relativeFromNow } from "@/lib/time";
import type { CaseDetail, PolicyDecision } from "@/lib/types";
import "./ReviewContext.css";

/**
 * Human review context.
 *
 * Shows why the case reached a human, where the decision stands, and — for a
 * pending review — routes to the review workspace rather than offering the
 * decision inline. Approval creates a proposed action; the executor performs
 * the dispatch. The wording keeps that boundary visible.
 */

function escalationReason(policy: PolicyDecision[]): PolicyDecision | undefined {
  return [...policy].reverse().find((p) => p.verdict === "ESCALATE");
}

const REASON_TEXT: Record<string, string> = {
  policy_escalate_budget: "Every permitted attempt was used without recovering the payment.",
  policy_escalate_ambiguity: "The failure cause was unmapped and history conflicted, so the system refused to guess.",
  action_deadline_expired: "The payment window closed without a payment. An expired link is not evidence the customer did not pay.",
  ttl_exhausted: "The case ran out of time before it could be resolved.",
};

export function ReviewContext({ detail }: { detail: CaseDetail }) {
  const reviews = detail.reviews;
  if (reviews.length === 0) {
    return (
      <p className="review__none">
        This case has not been escalated to a human.
      </p>
    );
  }

  const reason = escalationReason(detail.policy_decisions);

  return (
    <div className="review">
      {reason && (
        <div className="review__why">
          <p className="u-label">Why a human was needed</p>
          <p className="review__why-text">
            {REASON_TEXT[reason.reason_code] ?? "The system declined to act automatically."}
          </p>
          <p className="review__why-code u-mono">{reason.reason_code}</p>
        </div>
      )}

      <ul className="review__list">
        {reviews.map((r) => {
          const pending = r.status === "PENDING";
          const expiry = deadlineDistance(r.review_expires_at);
          return (
            <li key={r.id} className={`review__item${pending ? " is-pending" : ""}`}>
              <div className="review__head">
                <span className={`review__status status-${r.status.toLowerCase()}`}>
                  {r.status}
                </span>
                {r.selected_action && (
                  <span className="review__action u-mono">{r.selected_action}</span>
                )}
                <span className="review__time" title={absolute(r.created_at)}>
                  opened {relativeFromNow(r.created_at)}
                </span>
              </div>

              <dl className="review__facts">
                <div>
                  <dt>Reviewer</dt>
                  <dd>{r.reviewer_ref ?? "—"}</dd>
                </div>
                <div>
                  <dt>Decided</dt>
                  <dd title={r.decided_at ? absolute(r.decided_at) : undefined}>
                    {r.decided_at ? relativeFromNow(r.decided_at) : "Not yet"}
                  </dd>
                </div>
                <div>
                  <dt>{pending ? "Expires" : "Expiry"}</dt>
                  <dd className={pending && expiry?.expired ? "is-warn" : undefined}>
                    {expiry ? expiry.text : "—"}
                  </dd>
                </div>
              </dl>

              {pending ? (
                <div className="review__cta">
                  <Link className="btn btn--consequential" to={`/reviews/${detail.case.case_id}`}>
                    Open review workspace
                  </Link>
                  <p className="review__cta-note">
                    Approving proposes an action for the executor to dispatch under the
                    existing safeguards. It does not move money directly.
                  </p>
                </div>
              ) : (
                r.status === "APPROVED" && (
                  <p className="review__decided-note">
                    A recovery action was proposed. The executor dispatches it; the
                    reviewer does not.
                  </p>
                )
              )}
            </li>
          );
        })}
      </ul>
    </div>
  );
}
