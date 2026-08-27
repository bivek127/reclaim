import { Link } from "react-router-dom";
import type { Overview, SystemStatus as SystemStatusData } from "@/lib/types";
import "./SystemStatus.css";

interface Props {
  overview: Overview;
  system: SystemStatusData | undefined;
  environment: string | undefined;
  /** True while the console cannot reach the recovery service at all. */
  unreachable: boolean;
}

/**
 * What this console is attached to and whether the machine is running.
 *
 * Only states the backend actually reports. It says nothing about provider
 * health, because no endpoint supplies it — claiming a provider is healthy
 * would be an invention.
 */
export function SystemStatus({ overview, system, environment, unreachable }: Props) {
  const breakerOpen = overview.breaker_state === "OPEN";

  return (
    <div className="sys">
      <div className={`sys__row${unreachable ? " is-bad" : ""}`}>
        <span className="sys__marker" aria-hidden="true" />
        <span className="sys__label">Recovery service</span>
        <span className="sys__value">{unreachable ? "Unreachable" : "Responding"}</span>
      </div>

      <div className={`sys__row${breakerOpen ? " is-bad" : ""}`}>
        <span className="sys__marker" aria-hidden="true" />
        <span className="sys__label">Circuit breaker</span>
        <span className="sys__value">
          {overview.breaker_state}
          {overview.breaker_consecutive_failures > 0 && (
            <span className="sys__sub"> · {overview.breaker_consecutive_failures} consecutive failures</span>
          )}
        </span>
      </div>

      {breakerOpen && (
        <p className="sys__warn">
          Dispatch is stopped. Cases reaching the executor will halt rather than
          contact the provider.
        </p>
      )}

      {environment && (
        <div className="sys__row">
          <span className="sys__marker" aria-hidden="true" />
          <span className="sys__label">Environment</span>
          <span className="sys__value u-mono">{environment}</span>
        </div>
      )}

      {system && (
        <dl className="sys__stats">
          <div>
            <dt>Open actions</dt>
            <dd>{system.open_actions}</dd>
          </div>
          <div>
            <dt>Unresolved attempts</dt>
            <dd className={system.unresolved_attempts > 0 ? "is-note" : undefined}>
              {system.unresolved_attempts}
            </dd>
          </div>
          <div>
            <dt>Expired leases</dt>
            <dd>{system.leases_expired}</dd>
          </div>
          <div>
            <dt>Stale writes rejected</dt>
            <dd>{system.stale_writes_rejected}</dd>
          </div>
        </dl>
      )}

      <Link className="sys__link" to="/system">Operational detail</Link>
    </div>
  );
}
