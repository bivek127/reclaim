import { useState } from "react";
import { Identifier } from "../Identifier";
import { Money } from "../Money";
import { TechnicalRows } from "../Disclosure";
import { StatusBadge } from "../StatusBadge";
import { absolute } from "@/lib/time";
import { CATEGORIES, categoryOf, summaryOf, titleOf, toneOf } from "@/lib/events";
import type { AuditEvent } from "@/lib/types";
import "./TimelineEvent.css";

interface Props {
  event: AuditEvent;
  /** 1-based position in the authoritative ordering. */
  position: number;
  /** True when the previous event carries the same instant. */
  sharesInstant: boolean;
  clockTime: string;
}

const MONEY_KEYS = new Set(["amount_minor", "verified_amount_minor", "amount_paid_minor"]);
const ID_KEYS = new Set([
  "idempotency_key", "provider_reference", "reference_id", "diagnosis_id",
  "review_id", "policy_decision_id", "action_id", "webhook_event_id",
]);

function prettyKey(key: string): string {
  return key.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase());
}

/**
 * One audit event.
 *
 * Collapsed it answers what happened and when; expanded it yields the
 * identifiers, versions, and structured payload an investigator needs. Raw
 * JSON is never the first thing on screen.
 */
export function TimelineEvent({ event, position, sharesInstant, clockTime }: Props) {
  const [open, setOpen] = useState(false);
  const category = categoryOf(event);
  const meta = CATEGORIES[category];
  const tone = toneOf(event);
  const summary = summaryOf(event);
  const detail = event.detail ?? {};
  const detailEntries = Object.entries(detail);

  const currency =
    typeof detail["currency"] === "string" ? (detail["currency"] as string) : null;

  return (
    <li className={`tev tev--${tone}`}>
      <div className="tev__rail" aria-hidden="true">
        <span className={`tev__dot tev__dot--${category}`} />
      </div>

      <div className="tev__body">
        <div className="tev__top">
          <span className={`tev__cat tev__cat--${category}`} title={meta.blurb}>
            {meta.label}
          </span>
          <h3 className="tev__title">{titleOf(event)}</h3>
          <span className="tev__clock" title={absolute(event.occurred_at)}>
            {clockTime}
          </span>
        </div>

        {event.event_type === "state_transition" && (
          <p className="tev__states">
            <span className="tev__from u-mono">{event.prev_state ?? "—"}</span>
            <span className="tev__arrow" aria-hidden="true">→</span>
            <StatusBadge state={event.new_state ?? ""} variant="code" />
          </p>
        )}

        {summary && <p className="tev__summary">{summary}</p>}

        <p className="tev__meta">
          {event.reason_code && <span className="u-mono">{event.reason_code}</span>}
          {event.worker_id && <span>{event.worker_id}</span>}
          {event.fencing_token !== null && <span>token {event.fencing_token}</span>}
          {sharesInstant && (
            <span
              className="tev__tie"
              title="Written in the same transaction as the previous event, so the recorded order is by sequence, not clock."
            >
              same instant · #{position}
            </span>
          )}
        </p>

        <button
          type="button"
          className="tev__toggle"
          aria-expanded={open}
          onClick={() => setOpen((v) => !v)}
        >
          <span className={`tev__caret${open ? " is-open" : ""}`} aria-hidden="true" />
          Technical details
        </button>

        {open && (
          <div className="tev__tech">
            <TechnicalRows
              rows={[
                ["Event id", String(event.id)],
                ["Sequence position", `#${position}`],
                ["Exact time", absolute(event.occurred_at)],
                ["Event type", <span key="t" className="u-mono">{event.event_type}</span>],
                ...(event.obligation_id !== null
                  ? [["Obligation id", String(event.obligation_id)] as [string, React.ReactNode]] : []),
                ...(event.action_id !== null
                  ? [["Action id", String(event.action_id)] as [string, React.ReactNode]] : []),
                ...(event.attempt_id !== null
                  ? [["Attempt id", String(event.attempt_id)] as [string, React.ReactNode]] : []),
                ...(event.provider_request_id !== null
                  ? [["Provider request id", String(event.provider_request_id)] as [string, React.ReactNode]] : []),
                ...(event.provider_correlation_id
                  ? [["Correlation id", <Identifier key="c" value={event.provider_correlation_id} />] as [string, React.ReactNode]] : []),
                ...(event.policy_version
                  ? [["Policy version", event.policy_version] as [string, React.ReactNode]] : []),
                ...(event.model
                  ? [["Model", `${event.model}${event.model_version ? ` (${event.model_version})` : ""}`] as [string, React.ReactNode]] : []),
                ...(event.reviewer_ref
                  ? [["Reviewer", event.reviewer_ref] as [string, React.ReactNode]] : []),
              ]}
            />

            {detailEntries.length > 0 && (
              <>
                <p className="tev__tech-head">Recorded evidence</p>
                <TechnicalRows
                  rows={detailEntries.map(([key, value]) => {
                    if (MONEY_KEYS.has(key) && typeof value === "number" && currency) {
                      return [
                        prettyKey(key),
                        <Money key={key} minorUnits={value} currency={currency} />,
                      ] as [string, React.ReactNode];
                    }
                    if (ID_KEYS.has(key) && value !== null && value !== undefined) {
                      return [
                        prettyKey(key),
                        <Identifier key={key} value={String(value)} />,
                      ] as [string, React.ReactNode];
                    }
                    return [
                      prettyKey(key),
                      typeof value === "object" && value !== null ? (
                        <pre key={key} className="tech__json">{JSON.stringify(value, null, 2)}</pre>
                      ) : (
                        String(value)
                      ),
                    ] as [string, React.ReactNode];
                  })}
                />
              </>
            )}
          </div>
        )}
      </div>
    </li>
  );
}
