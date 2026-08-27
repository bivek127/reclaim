import { Link, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { PageHeader } from "@/components/PageHeader";
import { Section } from "@/components/Section";
import { Money } from "@/components/Money";
import { StatusBadge } from "@/components/StatusBadge";
import { Identifier } from "@/components/Identifier";
import { EmptyState, ErrorState, SkeletonBlock } from "@/components/States";
import { DecisionPanel } from "@/components/review/DecisionPanel";
import { ObligationPanel } from "@/components/case/ObligationPanel";
import { RecoveryProgress } from "@/components/case/RecoveryProgress";
import { VerificationPanel } from "@/components/case/VerificationPanel";
import { DecisionChain, DiagnosisTrail, PolicyTrail } from "@/components/case/DecisionTrail";
import { api, ApiError } from "@/lib/api";
import { absolute } from "@/lib/time";
import type { PolicyDecision } from "@/lib/types";
import "./ReviewDetailPage.css";
import { casePath, caseTimelinePath, reviewsPath } from "@/lib/routes";

const REASON_TEXT: Record<string, string> = {
  policy_escalate_budget:
    "Every permitted attempt was used and none recovered the payment, so the policy table stopped and asked for a human.",
  policy_escalate_ambiguity:
    "The failure cause was not in the policy table and the customer's history conflicted, so the system refused to guess.",
  action_deadline_expired:
    "The payment window closed without a payment. An expired link is not evidence that the customer did not pay, so the case was escalated rather than failed.",
  ttl_exhausted:
    "The case ran out of its time budget before the outcome could be resolved.",
};

function escalatingDecision(policy: PolicyDecision[]): PolicyDecision | undefined {
  return [...policy].reverse().find((p) => p.verdict === "ESCALATE");
}

/**
 * The review workspace.
 *
 * Evidence occupies the main column and the decision sits in a narrow rail, so
 * a reviewer reads before they act. Every panel here is the same component the
 * case investigation uses, which keeps one account of the evidence rather than
 * a second, review-specific interpretation of it.
 */
export function ReviewDetailPage() {
  const { caseId } = useParams();
  const id = Number(caseId);

  const detail = useQuery({
    queryKey: ["case", id],
    queryFn: () => api.case(id),
    enabled: Number.isFinite(id),
    retry: (count, error) => !(error instanceof ApiError && error.status === 404) && count < 1,
  });

  // Supplies the authoritative list of actions a reviewer may propose.
  const evidence = useQuery({
    queryKey: ["review", id],
    queryFn: () => api.reviewEvidence(id),
    enabled: Number.isFinite(id) && detail.isSuccess,
    retry: false,
  });

  if (!Number.isFinite(id)) {
    return (
      <>
        <PageHeader title="Review" />
        <div className="page-body"><EmptyState title="That is not a valid case reference" /></div>
      </>
    );
  }

  if (detail.isPending) {
    return (
      <>
        <PageHeader title={`Review · case #${id}`} />
        <div className="page-body">
          <SkeletonBlock height={120} />
          <div style={{ height: "var(--s-4)" }} />
          <SkeletonBlock height={340} />
        </div>
      </>
    );
  }

  if (detail.isError) {
    const notFound = detail.error instanceof ApiError && detail.error.status === 404;
    return (
      <>
        <PageHeader title={`Review · case #${id}`} />
        <div className="page-body">
          {notFound ? (
            <EmptyState
              title={`No case #${id}`}
              description="It may have been removed, or the link may be wrong."
              action={<Link className="btn btn--secondary" to={reviewsPath()}>Back to reviews</Link>}
            />
          ) : (
            <ErrorState title="Could not load this review" error={detail.error} onRetry={() => detail.refetch()} />
          )}
        </div>
      </>
    );
  }

  const data = detail.data;
  const c = data.case;
  // The open review if there is one, otherwise the most recent decision.
  const review = data.reviews.find((r) => r.status === "PENDING") ?? data.reviews.at(-1);

  if (!review) {
    return (
      <>
        <PageHeader title={`Case #${id}`} />
        <div className="page-body">
          <EmptyState
            title="This case has never been escalated"
            description="Only cases the system handed to a human appear in the review workspace."
            action={<Link className="btn btn--secondary" to={casePath(id)}>Open the case instead</Link>}
          />
        </div>
      </>
    );
  }

  const reason = escalatingDecision(data.policy_decisions);
  const reviewable =
    (Array.isArray(evidence.data?.["reviewable_actions"])
      ? (evidence.data["reviewable_actions"] as string[])
      : []);
  const failureCodes = Array.isArray(evidence.data?.["failure_codes"])
    ? (evidence.data["failure_codes"] as string[])
    : [];
  const proposed = data.actions.find((a) => a.status === "PROPOSED");

  return (
    <>
      <header className="rv-head">
        <nav className="rv-head__crumbs" aria-label="Breadcrumb">
          <Link to={reviewsPath()}>Reviews</Link>
          <span aria-hidden="true">/</span>
          <span aria-current="page">Case #{c.case_id}</span>
        </nav>
        <div className="rv-head__row">
          <div>
            <div className="rv-head__title-row">
              <h1 className="rv-head__title">Review · case #{c.case_id}</h1>
              <StatusBadge state={c.state} />
              <span className={`rv-head__status status-${review.status.toLowerCase()}`}>
                {review.status}
              </span>
            </div>
            <dl className="rv-head__facts">
              <div><dt>Customer</dt><dd className="u-mono">{c.customer_ref}</dd></div>
              <div><dt>Order</dt><dd className="u-mono">{c.anchor_key}</dd></div>
              <div><dt>Opened for review</dt><dd>{absolute(review.created_at)}</dd></div>
            </dl>
          </div>
          <div className="rv-head__links">
            <Link className="btn btn--sm" to={casePath(id)}>Full investigation</Link>
            <Link className="btn btn--sm" to={caseTimelinePath(id)}>Events &amp; logs</Link>
          </div>
        </div>
      </header>

      <div className="rv-body">
        <div className="rv-body__main">
          <Section
            title="Why this needs a human"
            note="The system declined to act automatically. This is the decision that stopped it."
          >
            {reason ? (
              <div className="rv-why">
                <p className="rv-why__text">
                  {REASON_TEXT[reason.reason_code] ??
                    "The policy table refused to authorise an automatic action."}
                </p>
                <dl className="rv-why__facts">
                  <div><dt>Reason code</dt><dd className="u-mono">{reason.reason_code}</dd></div>
                  <div><dt>Policy version</dt><dd className="u-mono">{reason.policy_version}</dd></div>
                  <div>
                    <dt>Attempts used</dt>
                    <dd>{c.attempt_count} of {c.max_attempts}</dd>
                  </div>
                </dl>
                {failureCodes.length > 0 && (
                  <div className="rv-why__codes">
                    <p className="u-label">Failure signals diagnosis was given</p>
                    <ul>
                      {failureCodes.map((code, i) => (
                        <li key={`${code}-${i}`}><Identifier value={code} /></li>
                      ))}
                    </ul>
                    <p className="rv-why__codes-note">
                      Taken from this case's own provider requests. Where the provider
                      returned no error code, the request outcome stands in its place.
                    </p>
                  </div>
                )}
              </div>
            ) : (
              <p className="rv-empty">
                No escalating policy decision is recorded for this case, so the reason it
                reached review cannot be shown from policy data alone. The audit trail is
                the authority here.
              </p>
            )}
          </Section>

          <Section
            title="What approval would authorise"
            tone="evidence"
            note="Approval proposes an action. The executor dispatches it, subject to the same safeguards as any automatic recovery."
          >
            {proposed ? (
              <div className="rv-proposed">
                <p className="rv-proposed__title">
                  Already proposed: <span className="u-mono">{proposed.action_type}</span>
                </p>
                <p className="rv-proposed__body">
                  A reviewer has already approved this case. The action is waiting for the
                  executor to dispatch it.
                </p>
              </div>
            ) : (
              <ul className="rv-authorise">
                <li>
                  Creates a <strong>PROPOSED</strong> recovery action on this case — not a
                  payment.
                </li>
                <li>
                  The executor picks it up and performs the dispatch under the attempt
                  budget ({c.attempt_count} of {c.max_attempts} used), idempotency keys,
                  and the circuit breaker.
                </li>
                <li>
                  Revenue is only recognised later, and only if two independent sources
                  agree.
                </li>
              </ul>
            )}
          </Section>

          <Section
            title="Recovery so far"
            note="What was already attempted, and what the provider said about it."
          >
            <RecoveryProgress detail={data} />
          </Section>

          <Section
            title="Independent verification"
            tone="evidence"
            note="Provider claims are evidence. Recovery is recognised only where two independently sourced pieces of evidence agree."
          >
            <VerificationPanel detail={data} />
          </Section>

          <Section
            title="Policy decision"
            note="Deterministic. This is what authorised or refused each action."
          >
            <DecisionChain detail={data} />
            <PolicyTrail decisions={data.policy_decisions} />
          </Section>

          <Section
            title="Diagnosis"
            tone="advisory"
            note="Advisory input only. It classifies the failure; it authorises nothing, and its confidence does not influence policy or this decision."
          >
            <DiagnosisTrail diagnoses={data.diagnoses} />
          </Section>
        </div>

        <aside className="rv-body__side" aria-label="Review decision">
          <DecisionPanel
            detail={data}
            review={review}
            reviewableActions={reviewable}
            onDecided={() => {
              detail.refetch();
              evidence.refetch();
            }}
          />

          <Section title="Financial obligation" note="Persisted facts about the money owed.">
            <ObligationPanel detail={data} />
          </Section>

          <div className="rv-side-note">
            <p className="u-label">Amount under review</p>
            <Money minorUnits={c.amount_minor} currency={c.currency} size="strong" showCode />
            <p className="rv-side-note__text">
              Nothing on this screen moves money. Approval proposes work; the executor
              performs it.
            </p>
          </div>
        </aside>
      </div>
    </>
  );
}
