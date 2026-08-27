import { Link, useLocation, useParams } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { CaseHeader } from "@/components/case/CaseHeader";
import { ObligationPanel } from "@/components/case/ObligationPanel";
import { LifecycleChain } from "@/components/case/LifecycleChain";
import { RecoveryProgress } from "@/components/case/RecoveryProgress";
import { VerificationPanel } from "@/components/case/VerificationPanel";
import { DecisionChain, DiagnosisTrail, PolicyTrail } from "@/components/case/DecisionTrail";
import { ReviewContext } from "@/components/case/ReviewContext";
import { Section } from "@/components/Section";
import { EmptyState, ErrorState, SkeletonBlock } from "@/components/States";
import { PageHeader } from "@/components/PageHeader";
import { api, ApiError } from "@/lib/api";
import "./CaseDetailPage.css";

/**
 * The case investigation workspace.
 *
 * Reading order follows the investigation, not the schema: what is this and
 * what is it worth, how did it get here, what was actually attempted, was the
 * result independently verified, did a human intervene, and only then why the
 * system chose what it chose. Advisory input sits last and lowest because it
 * carries the least authority.
 */
export function CaseDetailPage() {
  const { caseId } = useParams();
  const location = useLocation();
  const id = Number(caseId);
  const onEvents = location.pathname.endsWith("/timeline");

  const detail = useQuery({
    queryKey: ["case", id],
    queryFn: () => api.case(id),
    enabled: Number.isFinite(id),
    retry: (count, error) => !(error instanceof ApiError && error.status === 404) && count < 1,
  });

  // The lifecycle is read from the audit trail, the same source the forensic
  // timeline uses. It is never reconstructed from the domain tables.
  const history = useQuery({
    queryKey: ["timeline", id],
    queryFn: () => api.timeline(id),
    enabled: Number.isFinite(id) && detail.isSuccess,
    retry: false,
  });

  if (!Number.isFinite(id)) {
    return (
      <>
        <PageHeader title="Case" />
        <div className="page-body">
          <EmptyState title="That is not a valid case reference" />
        </div>
      </>
    );
  }

  if (detail.isPending) {
    return (
      <>
        <PageHeader title={`Case #${id}`} />
        <div className="page-body">
          <SkeletonBlock height={140} />
          <div style={{ height: "var(--s-4)" }} />
          <SkeletonBlock height={320} />
        </div>
      </>
    );
  }

  if (detail.isError) {
    const notFound = detail.error instanceof ApiError && detail.error.status === 404;
    return (
      <>
        <PageHeader title={`Case #${id}`} />
        <div className="page-body">
          {notFound ? (
            <EmptyState
              title={`No case #${id}`}
              description="It may have been removed, or the link may be wrong."
              action={<Link className="btn btn--secondary" to="/cases">Back to cases</Link>}
            />
          ) : (
            <ErrorState
              title="Could not load this case"
              error={detail.error}
              onRetry={() => detail.refetch()}
            />
          )}
        </div>
      </>
    );
  }

  const data = detail.data;
  const gaps = history.data?.unreconstructable ?? [];

  return (
    <>
      <CaseHeader detail={data} />

      <nav className="case-tabs" aria-label="Case views">
        <Link
          to={`/cases/${id}`}
          className={`case-tabs__tab${!onEvents ? " is-active" : ""}`}
          aria-current={!onEvents ? "page" : undefined}
        >
          Overview
        </Link>
        <Link
          to={`/cases/${id}/timeline`}
          className={`case-tabs__tab${onEvents ? " is-active" : ""}`}
          aria-current={onEvents ? "page" : undefined}
        >
          Events &amp; logs
          {history.data && (
            <span className="case-tabs__count">{history.data.timeline.length}</span>
          )}
        </Link>
      </nav>

      {onEvents ? (
        <div className="page-body">
          <Section
            title="Events &amp; logs"
            note="Reconstructed from the case's audit trail alone."
          >
            {history.isPending ? (
              <SkeletonBlock height={160} />
            ) : history.isError ? (
              <ErrorState error={history.error} onRetry={() => history.refetch()} />
            ) : (
              <div className="events-preview">
                <p className="events-preview__count">
                  <strong>{history.data?.timeline.length ?? 0}</strong> audit events ·{" "}
                  <strong>{history.data?.state_changes.length ?? 0}</strong> state changes ·{" "}
                  <strong>{history.data?.stale_writes.length ?? 0}</strong> rejected stale writes
                </p>
                <LifecycleChain
                  changes={history.data?.state_changes ?? []}
                  currentState={data.case.state}
                />
                <p className="events-preview__note">
                  The full forensic timeline — every event with its worker, fencing token,
                  and structured payload — is the next surface to be built.
                </p>
              </div>
            )}
          </Section>
        </div>
      ) : (
        <div className="case-body">
          <aside className="case-body__side" aria-label="Financial obligation">
            <Section title="Financial obligation" note="Persisted facts about the money owed.">
              <ObligationPanel detail={data} />
            </Section>
          </aside>

          <div className="case-body__main">
            {gaps.length > 0 && (
              <div className="evidence-gap" role="note">
                <p className="evidence-gap__title">Evidence gap</p>
                <p className="evidence-gap__body">
                  The audit trail cannot supply:{" "}
                  {gaps.map((g) => (
                    <code key={g} className="u-mono">{g}</code>
                  ))}
                  . This is reported rather than hidden — absence of evidence is itself
                  evidence.
                </p>
              </div>
            )}

            <Section
              title="How this case reached its current state"
              note="Every transition below comes from the audit trail."
              aside={
                <Link className="btn btn--sm" to={`/cases/${id}/timeline`}>
                  Events &amp; logs
                </Link>
              }
            >
              <LifecycleChain
                changes={history.data?.state_changes ?? []}
                currentState={data.case.state}
                loading={history.isPending}
              />
            </Section>

            <Section
              title="Recovery progress"
              note="Action, attempt, and provider call — the mechanism the system opened and what happened to it."
            >
              <RecoveryProgress detail={data} />
            </Section>

            <Section
              title="Independent verification"
              tone="evidence"
              note="Recovery is recognised only where two independently sourced pieces of evidence agree."
            >
              <VerificationPanel detail={data} />
            </Section>

            {data.reviews.length > 0 && (
              <Section title="Human review" note="A controlled safety boundary, not a bypass.">
                <ReviewContext detail={data} />
              </Section>
            )}

            <Section
              title="Policy decision"
              note="Deterministic. This is what authorised — or refused — a recovery action."
            >
              <DecisionChain detail={data} />
              <PolicyTrail decisions={data.policy_decisions} />
            </Section>

            <Section
              title="Diagnosis"
              tone="advisory"
              note="Advisory input only. It classifies the failure; it does not authorise anything, and its confidence does not influence policy."
            >
              <DiagnosisTrail diagnoses={data.diagnoses} />
            </Section>
          </div>
        </div>
      )}
    </>
  );
}
