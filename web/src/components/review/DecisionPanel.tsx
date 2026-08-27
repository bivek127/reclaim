import { useEffect, useRef, useState } from "react";
import { Link } from "react-router-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Money } from "../Money";
import { api, ApiError } from "@/lib/api";
import { useReviewer } from "@/hooks/useReviewer";
import { absolute, deadlineDistance } from "@/lib/time";
import type { CaseDetail, HumanReview } from "@/lib/types";
import "./DecisionPanel.css";
import { caseTimelinePath } from "@/lib/routes";
import { StatusBadge } from "@/components/StatusBadge";

interface Props {
  detail: CaseDetail;
  review: HumanReview;
  reviewableActions: string[];
  onDecided: () => void;
}

type Pending = "approve" | "reject" | null;

/**
 * The review decision.
 *
 * Nothing here changes case state on its own: both paths post to the domain
 * and render whatever it returns. There is no optimistic update, because a
 * case that moved under the reviewer must not briefly appear decided.
 *
 * A decision is only offered while the review row is still PENDING. A lapsed
 * deadline is surfaced as a warning rather than a lock, because the backend
 * still accepts a decision until the expiry sweep closes the review — telling
 * the reviewer otherwise would be inventing a rule the system does not have.
 */
export function DecisionPanel({ detail, review, reviewableActions, onDecided }: Props) {
  const queryClient = useQueryClient();
  const { reviewer, setReviewer, hasReviewer } = useReviewer();
  const [action, setAction] = useState("");
  const [confirming, setConfirming] = useState<Pending>(null);
  const confirmRef = useRef<HTMLButtonElement>(null);

  const decided = review.status !== "PENDING";
  const expiry = deadlineDistance(review.review_expires_at);
  const lapsed = !decided && expiry?.expired === true;

  // The permitted actions arrive with the review evidence, after first render.
  // Default to the first one once it is known, without overriding a choice the
  // reviewer has already made.
  useEffect(() => {
    setAction((current) =>
      current && reviewableActions.includes(current) ? current : reviewableActions[0] ?? "",
    );
  }, [reviewableActions]);

  useEffect(() => {
    if (confirming) confirmRef.current?.focus();
  }, [confirming]);

  const decision = useMutation({
    mutationFn: (kind: Exclude<Pending, null>) =>
      kind === "approve"
        ? api.approve(detail.case.case_id, reviewer.trim(), action)
        : api.reject(detail.case.case_id, reviewer.trim()),
    onSuccess: () => {
      // Re-read from the server rather than assuming the outcome.
      queryClient.invalidateQueries({ queryKey: ["case", detail.case.case_id] });
      queryClient.invalidateQueries({ queryKey: ["review", detail.case.case_id] });
      queryClient.invalidateQueries({ queryKey: ["timeline", detail.case.case_id] });
      queryClient.invalidateQueries({ queryKey: ["reviews"] });
      setConfirming(null);
      onDecided();
    },
  });

  const conflict = decision.error instanceof ApiError && decision.error.isConflict;
  // A fault is not a refusal: the service never told us what it decided, so the
  // outcome is unknown and must be re-read rather than asserted either way.
  const fault = decision.error instanceof ApiError && decision.error.isServerFault;
  const busy = decision.isPending;

  if (decided) {
    return (
      <div className="dp dp--closed">
        <p className="u-label">Decision</p>
        <p className={`dp__outcome status-${review.status.toLowerCase()}`}>{review.status}</p>
        <dl className="dp__facts">
          <div>
            <dt>Reviewer</dt>
            <dd>{review.reviewer_ref ?? "—"}</dd>
          </div>
          <div>
            <dt>Decided</dt>
            <dd>{review.decided_at ? absolute(review.decided_at) : "—"}</dd>
          </div>
          {review.selected_action && (
            <div>
              <dt>Action proposed</dt>
              <dd className="u-mono">{review.selected_action}</dd>
            </div>
          )}
          <div>
            <dt>Resulting case state</dt>
            <dd><StatusBadge state={detail.case.state} /></dd>
          </div>
        </dl>
        <p className="dp__note">
          This review is closed. Any further change to the case happens through the
          normal recovery path, not through this screen.
        </p>
        <Link className="btn btn--sm" to={caseTimelinePath(detail.case.case_id)}>
          See it in the audit trail
        </Link>
      </div>
    );
  }

  return (
    <div className="dp">
      <div className="dp__head">
        <p className="u-label">Decision required</p>
        <p className={`dp__expiry${lapsed ? " is-lapsed" : ""}`} title={absolute(review.review_expires_at)}>
          {lapsed ? "Deadline passed" : `Decide ${expiry?.text ?? "soon"}`}
        </p>
      </div>

      {lapsed && (
        <p className="dp__warn" role="note">
          This review is past its deadline but still open. The expiry job may close it
          at any moment, in which case a decision here will be refused.
        </p>
      )}

      <div className="dp__amount">
        <p className="u-label">Amount at stake</p>
        <Money minorUnits={detail.case.amount_minor} currency={detail.case.currency} size="strong" showCode />
      </div>

      <div className="dp__field">
        <label className="dp__label" htmlFor="reviewer-ref">Your reviewer reference</label>
        <input
          id="reviewer-ref"
          className="input"
          type="text"
          value={reviewer}
          placeholder="e.g. ops.name@company.com"
          onChange={(e) => setReviewer(e.target.value)}
          disabled={busy}
          aria-describedby="reviewer-hint"
        />
        <p className="dp__hint" id="reviewer-hint">
          Recorded against this decision in the audit trail. This is a label, not a
          sign-in — this system has no authentication layer.
        </p>
      </div>

      <fieldset className="dp__field" disabled={busy}>
        <legend className="dp__label">Action to propose</legend>
        {reviewableActions.length === 0 ? (
          <p className="dp__hint">No dispatchable action is available for this case.</p>
        ) : (
          reviewableActions.map((value) => (
            <label className="dp__radio" key={value}>
              <input
                type="radio"
                name="review-action"
                value={value}
                checked={action === value}
                onChange={() => setAction(value)}
              />
              <span className="u-mono">{value}</span>
            </label>
          ))
        )}
        <p className="dp__hint">
          Only actions the executor can actually dispatch are offered.
        </p>
      </fieldset>

      {decision.isError && (
        <div className="dp__error" role="alert">
          <p className="dp__error-title">
            {conflict
              ? "This case changed"
              : fault
                ? "The service failed while recording this decision"
                : "The decision was not applied"}
          </p>
          <p className="dp__error-body">
            {decision.error instanceof Error ? decision.error.message : "Unknown failure."}
          </p>
          {fault && (
            <p className="dp__error-body">
              Whether it was recorded is unknown from here. Reload this review and
              read the current state before deciding again.
            </p>
          )}
          <button
            type="button"
            className="btn btn--sm"
            onClick={() => { decision.reset(); onDecided(); }}
          >
            Reload this review
          </button>
        </div>
      )}

      {confirming === null ? (
        <div className="dp__actions">
          <button
            type="button"
            className="btn btn--consequential dp__approve"
            disabled={busy || !hasReviewer || !action}
            onClick={() => setConfirming("approve")}
          >
            Approve
          </button>
          <button
            type="button"
            className="btn btn--danger"
            disabled={busy || !hasReviewer}
            onClick={() => setConfirming("reject")}
          >
            Reject
          </button>
          {!hasReviewer && (
            <p className="dp__hint dp__hint--block">
              Enter your reviewer reference to make a decision.
            </p>
          )}
        </div>
      ) : (
        <div className="dp__confirm" role="group" aria-live="polite" aria-label="Confirm decision">
          <p className="dp__confirm-title">
            {confirming === "approve" ? "Approve this recovery?" : "Reject this recovery?"}
          </p>
          <ul className="dp__consequences">
            {confirming === "approve" ? (
              <>
                <li>
                  A <span className="u-mono">{action}</span> action is <strong>proposed</strong> on
                  case #{detail.case.case_id}.
                </li>
                <li>
                  <strong>No money moves now.</strong> The executor dispatches it later,
                  under the existing attempt budget, idempotency, and breaker safeguards.
                </li>
                <li>The case stays escalated until the executor picks it up.</li>
              </>
            ) : (
              <>
                <li>
                  The review is recorded as <strong>rejected</strong>.
                </li>
                <li>
                  Case #{detail.case.case_id} moves to{" "}
                  <span className="u-mono">VERIFIED_FAILED</span> and is closed as not
                  recovered. This is terminal.
                </li>
                <li>No further recovery attempt will be made.</li>
              </>
            )}
            <li>Recorded against <strong>{reviewer.trim()}</strong> in the audit trail.</li>
          </ul>
          <div className="dp__actions">
            <button
              ref={confirmRef}
              type="button"
              className={`btn ${confirming === "approve" ? "btn--consequential" : "btn--danger"}`}
              disabled={busy}
              onClick={() => decision.mutate(confirming)}
            >
              {busy
                ? "Submitting…"
                : confirming === "approve"
                  ? `Yes, propose ${action}`
                  : "Yes, reject and close"}
            </button>
            <button
              type="button"
              className="btn btn--ghost"
              disabled={busy}
              onClick={() => setConfirming(null)}
            >
              Cancel
            </button>
          </div>
          {busy && (
            <p className="dp__hint" role="status">
              Waiting for the recovery service to confirm…
            </p>
          )}
        </div>
      )}
    </div>
  );
}
