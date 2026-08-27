import { Link } from "react-router-dom";
import { Money } from "../Money";
import { StatusBadge } from "../StatusBadge";
import { isTerminal, presentationFor } from "@/lib/states";
import { absolute, deadlineDistance, relativeFromNow } from "@/lib/time";
import type { CaseDetail } from "@/lib/types";
import "./CaseHeader.css";

interface Props {
  detail: CaseDetail;
}

/**
 * Establishes, in one glance: which case, what it is worth, what state it is
 * in, and whether it is waiting on a human.
 *
 * The obligation amount is the largest figure on the page. Where revenue has
 * actually been recognised it appears beside it, labelled as verified — the
 * two are never conflated.
 */
export function CaseHeader({ detail }: Props) {
  const c = detail.case;
  const { meaning, actionable } = presentationFor(c.state);
  // Once a case is terminal its payment window is history: showing a countdown
  // there implies something is still pending when nothing is.
  const terminal = isTerminal(c.state);
  const window = terminal ? null : deadlineDistance(c.action_deadline_at);
  const waiting = c.state === "AWAITING_CUSTOMER";
  const recovered = c.recovered_amount_minor > 0;
  const pendingReview = detail.reviews.find((r) => r.status === "PENDING");

  return (
    <header className="case-head">
      <nav className="case-head__crumbs" aria-label="Breadcrumb">
        <Link to="/cases">Cases</Link>
        <span aria-hidden="true">/</span>
        <span aria-current="page">Case #{c.case_id}</span>
      </nav>

      <div className="case-head__main">
        <div className="case-head__identity">
          <div className="case-head__title-row">
            <h1 className="case-head__title">Case #{c.case_id}</h1>
            <StatusBadge state={c.state} />
            {pendingReview && <span className="case-head__flag">Awaiting review</span>}
          </div>
          <p className="case-head__meaning">{meaning}</p>

          <dl className="case-head__facts">
            <div>
              <dt>Customer</dt>
              <dd className="u-mono">{c.customer_ref}</dd>
            </div>
            <div>
              <dt>{c.anchor_kind === "ORDER" ? "Order" : "Subscription cycle"}</dt>
              <dd className="u-mono">{c.anchor_key}</dd>
            </div>
            <div>
              <dt>Opened</dt>
              <dd title={absolute(c.created_at)}>{relativeFromNow(c.created_at)}</dd>
            </div>
            <div>
              <dt>Last activity</dt>
              <dd title={absolute(c.updated_at)}>{relativeFromNow(c.updated_at)}</dd>
            </div>
          </dl>
        </div>

        <div className="case-head__money">
          <p className="u-label">Obligation</p>
          <Money minorUnits={c.amount_minor} currency={c.currency} size="hero" showCode />
          {recovered ? (
            <p className="case-head__recovered">
              <span className="case-head__recovered-dot" aria-hidden="true" />
              <Money minorUnits={c.recovered_amount_minor} currency={c.currency} />
              <span>&nbsp;verified recovered</span>
            </p>
          ) : (
            <p className="case-head__unrecovered">No revenue recognised</p>
          )}

          {window && (
            <p
              className={`case-head__window${
                window.expired ? (waiting ? " is-urgent" : " is-passed") : " is-open"
              }`}
              title={absolute(c.action_deadline_at)}
            >
              Payment window {window.text}
            </p>
          )}
        </div>
      </div>

      {actionable && (
        <p className="case-head__callout" role="note">
          {pendingReview
            ? "A reviewer must decide on this case before recovery can continue."
            : "This case is waiting on a human. Review the evidence below before acting."}
        </p>
      )}
    </header>
  );
}
