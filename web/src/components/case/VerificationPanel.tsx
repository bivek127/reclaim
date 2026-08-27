import { Identifier } from "../Identifier";
import { Money } from "../Money";
import { Disclosure, TechnicalRows } from "../Disclosure";
import { absolute } from "@/lib/time";
import type { CaseDetail } from "@/lib/types";
import "./VerificationPanel.css";

/**
 * Independent verification.
 *
 * The two evidence sources are shown side by side and never merged: a webhook
 * is a claim the provider pushed to us, an independent query is an answer we
 * went and fetched. Recovery is recognised only where both agree and the paid
 * amount matches the attempt. A webhook alone is displayed as a claim, never
 * as a recovered outcome.
 */
export function VerificationPanel({ detail }: { detail: CaseDetail }) {
  const verifications = detail.verifications;

  if (verifications.length === 0) {
    const waiting = detail.case.state === "AWAITING_CUSTOMER";
    return (
      <div className="verify__empty">
        <p className="verify__empty-title">Not verified</p>
        <p className="verify__empty-body">
          {waiting
            ? "No verification has been recorded yet. A payment link being live is not evidence that anyone paid."
            : "No verification was recorded for this case, so no recovery can be claimed from this screen."}
        </p>
      </div>
    );
  }

  return (
    <ul className="verify">
      {verifications.map((v) => {
        const amountMatches = v.verified_amount_minor === detail.case.amount_minor;
        return (
          <li key={v.id} className={`verify__item${v.agrees ? " is-agreed" : " is-disagreed"}`}>
            <div className="verify__verdict">
              <span className="verify__verdict-label">
                {v.agrees ? "Independently verified" : "Sources disagree"}
              </span>
              {v.agrees ? (
                <Money minorUnits={v.verified_amount_minor} currency={detail.case.currency} size="strong" />
              ) : (
                <span className="verify__none">No revenue recognised</span>
              )}
            </div>

            <div className="verify__sources">
              <div className="verify__source">
                <p className="u-label">Source 1 · Webhook claim</p>
                <p className="verify__value">{v.webhook_status ?? "None received"}</p>
                <p className="verify__caption">Pushed to us by the provider.</p>
              </div>
              <div className="verify__arrow" aria-hidden="true" />
              <div className="verify__source">
                <p className="u-label">Source 2 · Independent query</p>
                <p className="verify__value">{v.query_status ?? "No answer"}</p>
                <p className="verify__caption">Fetched by us, not accepted on trust.</p>
              </div>
            </div>

            <dl className="verify__checks">
              <div>
                <dt>Correlated</dt>
                <dd>{v.query_correlation_id ? "Yes" : "No"}</dd>
              </div>
              <div>
                <dt>Amount matches obligation</dt>
                <dd className={amountMatches ? "is-ok" : "is-warn"}>
                  {amountMatches ? "Yes" : "No"}
                </dd>
              </div>
              <div>
                <dt>Both sources agree</dt>
                <dd className={v.agrees ? "is-ok" : "is-warn"}>{v.agrees ? "Yes" : "No"}</dd>
              </div>
            </dl>

            <Disclosure label="Verification details">
              <TechnicalRows
                rows={[
                  ["Verification id", String(v.id)],
                  ["Attempt id", String(v.attempt_id)],
                  ["Webhook event id", v.webhook_event_id ? String(v.webhook_event_id) : "—"],
                  ["Query correlation id", <Identifier key="c" value={v.query_correlation_id} />],
                  ["Verified amount (minor units)", String(v.verified_amount_minor)],
                  ["Recorded", absolute(v.created_at)],
                ]}
              />
            </Disclosure>
          </li>
        );
      })}
    </ul>
  );
}
