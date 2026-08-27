import { CATEGORIES, categoryOf, summaryOf, titleOf } from "@/lib/events";
import { absolute, relativeFromNow } from "@/lib/time";
import type { ActivityEvent, AuditEvent } from "@/lib/types";
import "./SystemEvents.css";

interface Props {
  /** The whole recent-activity window, unfiltered. Both figures are read from it. */
  activity: ActivityEvent[];
}

/** Activity rows carry a subset of audit columns; widen for the shared helpers. */
function asAuditEvent(event: ActivityEvent): AuditEvent {
  return {
    ...event,
    obligation_id: null, action_id: null, attempt_id: null,
    provider_request_id: null, fencing_token: null, model: null,
    model_version: null, policy_version: null, provider_correlation_id: null,
    detail: {},
  };
}

/**
 * Audit events that belong to no case.
 *
 * Breaker openings and resets are recorded with a null `case_id`, so they can
 * never appear in a case's forensic timeline and would otherwise be invisible.
 *
 * The only contract that returns them is the overview activity feed, which is
 * the most recent N events of every kind. Filtering it by `case_id` is honest
 * about what it found but cannot be complete: a breaker opening old enough to
 * fall outside that window is simply not in the response. The window is stated
 * on screen rather than implied, because an operator reading an empty list
 * needs to know whether it means "none happened" or "none recently".
 */
export function SystemEvents({ activity }: Props) {
  const global = activity.filter((e) => e.case_id === null);

  return (
    <>
      {global.length === 0 ? (
        <p className="sev__empty">
          No system-wide events in this window. The breaker has not opened or
          been reset within the {activity.length} most recent audit events.
        </p>
      ) : (
        <ul className="sev" aria-label="System-wide events">
          {global.map((event) => {
            const widened = asAuditEvent(event);
            const category = categoryOf(widened);
            return (
              <li className="sev__row" key={event.id}>
                <span className="sev__mark" aria-hidden="true" />
                <div className="sev__body">
                  <p className="sev__title">{titleOf(widened)}</p>
                  {summaryOf(widened) && <p className="sev__sum">{summaryOf(widened)}</p>}
                  <p className="sev__meta">
                    <span className="sev__cat">{CATEGORIES[category].label}</span>
                    <span className="sev__scope">No case — system-wide</span>
                    {event.reason_code && <span className="u-mono">{event.reason_code}</span>}
                    {event.worker_id && <span className="u-mono">{event.worker_id}</span>}
                    <span className="sev__time" title={absolute(event.occurred_at)}>
                      {relativeFromNow(event.occurred_at)}
                    </span>
                  </p>
                </div>
              </li>
            );
          })}
        </ul>
      )}

      <p className="sev__window">
        Read from the {activity.length} most recent audit events. This is not a
        complete breaker history — older system events fall outside the window.
      </p>
    </>
  );
}
