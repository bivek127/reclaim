import { Link } from "react-router-dom";
import { CATEGORIES, categoryOf, titleOf } from "@/lib/events";
import { absolute, relativeFromNow } from "@/lib/time";
import type { ActivityEvent, AuditEvent } from "@/lib/types";
import "./RecentActivity.css";
import { casePath } from "@/lib/routes";

/**
 * What changed across the estate most recently.
 *
 * Served whole by the overview endpoint — this is not stitched together from
 * per-case timelines. Routine lease bookkeeping is left out because it would
 * crowd out the events an operator would act on; the per-case audit trail
 * remains the complete record.
 */

/** The activity rows carry a subset of audit fields; widen for shared helpers. */
function asAuditEvent(event: ActivityEvent): AuditEvent {
  return {
    ...event,
    obligation_id: null, action_id: null, attempt_id: null,
    provider_request_id: null, fencing_token: null, model: null,
    model_version: null, policy_version: null, provider_correlation_id: null,
    detail: {},
  };
}

const ROUTINE = new Set(["lease_claimed", "lease_released"]);

export function RecentActivity({ events }: { events: ActivityEvent[] }) {
  const shown = events.filter((e) => !ROUTINE.has(e.event_type)).slice(0, 8);

  if (shown.length === 0) {
    return <p className="act__empty">No recent activity has been recorded.</p>;
  }

  return (
    <ul className="act">
      {shown.map((event) => {
        const widened = asAuditEvent(event);
        const category = categoryOf(widened);
        const transition = event.prev_state && event.new_state && event.prev_state !== event.new_state;
        return (
          <li className="act__row" key={event.id}>
            <span className={`act__dot act__dot--${category}`} aria-hidden="true" />
            <div className="act__body">
              <p className="act__line">
                <span className="act__what">{titleOf(widened)}</span>
                {event.case_id !== null ? (
                  <Link className="act__case" to={casePath(event.case_id)}>
                    case #{event.case_id}
                  </Link>
                ) : (
                  <span className="act__case act__case--system">system-wide</span>
                )}
              </p>
              <p className="act__meta">
                <span className="act__cat">{CATEGORIES[category].label}</span>
                {transition && (
                  <span className="u-mono act__states">
                    {event.prev_state} → {event.new_state}
                  </span>
                )}
                <span className="act__time" title={absolute(event.occurred_at)}>
                  {relativeFromNow(event.occurred_at)}
                </span>
              </p>
            </div>
          </li>
        );
      })}
    </ul>
  );
}
