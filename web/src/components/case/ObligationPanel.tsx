import { Money } from "../Money";
import { Identifier } from "../Identifier";
import { absolute, compact } from "@/lib/time";
import type { CaseDetail } from "@/lib/types";
import "./ObligationPanel.css";

/**
 * The financial obligation and case vitals.
 *
 * Tier 1 of the trust hierarchy: these are persisted facts about money owed,
 * not conclusions the system drew. They are rendered with the strongest
 * contrast on the page and are never placed below advisory content.
 */
export function ObligationPanel({ detail }: { detail: CaseDetail }) {
  const o = detail.obligation;
  const c = detail.case;
  const budgetSpent = c.attempt_count >= c.max_attempts;

  return (
    <div className="obl">
      <div className="obl__amount">
        <p className="u-label">Amount owed</p>
        <Money minorUnits={o.amount_minor} currency={o.currency} size="strong" showCode />
      </div>

      <dl className="kv obl__kv">
        <div className="kv__row">
          <dt className="kv__key">Anchor</dt>
          <dd className="kv__val">
            <Identifier value={o.anchor_canonical} label="Obligation anchor" />
          </dd>
        </div>
        <div className="kv__row">
          <dt className="kv__key">Customer</dt>
          <dd className="kv__val u-mono">{o.customer_ref}</dd>
        </div>
        <div className="kv__row">
          <dt className="kv__key">Source event</dt>
          <dd className="kv__val">
            <Identifier value={o.source_event_id} truncate label="Source event id" />
          </dd>
        </div>
        <div className="kv__row">
          <dt className="kv__key">First seen</dt>
          <dd className="kv__val" title={absolute(o.first_seen_at)}>{compact(o.first_seen_at)}</dd>
        </div>
        <div className="kv__row">
          <dt className="kv__key">Last seen</dt>
          <dd className="kv__val" title={absolute(o.last_seen_at)}>{compact(o.last_seen_at)}</dd>
        </div>
      </dl>

      <div className="obl__divider" role="separator" />

      <p className="u-label obl__vitals-label">Case vitals</p>
      <dl className="kv obl__kv">
        <div className="kv__row">
          <dt className="kv__key">Attempt budget</dt>
          <dd className={`kv__val obl__budget${budgetSpent ? " is-spent" : ""}`}>
            {c.attempt_count} of {c.max_attempts} used
            {budgetSpent && <span className="obl__budget-note"> — exhausted</span>}
          </dd>
        </div>
        <div className="kv__row">
          <dt className="kv__key">Recovered</dt>
          <dd className="kv__val">
            {c.recovered_amount_minor > 0 ? (
              <Money minorUnits={c.recovered_amount_minor} currency={o.currency} />
            ) : (
              <span className="obl__none">Nothing recognised</span>
            )}
          </dd>
        </div>
        <div className="kv__row">
          <dt className="kv__key">Opened</dt>
          <dd className="kv__val" title={absolute(c.created_at)}>{compact(c.created_at)}</dd>
        </div>
        <div className="kv__row">
          <dt className="kv__key">Updated</dt>
          <dd className="kv__val" title={absolute(c.updated_at)}>{compact(c.updated_at)}</dd>
        </div>
      </dl>
    </div>
  );
}
