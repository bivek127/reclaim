import { StatusBadge } from "../StatusBadge";
import { absolute, relativeFromNow } from "@/lib/time";
import type { StateChange } from "@/lib/types";
import "./LifecycleChain.css";

interface Props {
  changes: StateChange[];
  currentState: string;
  loading?: boolean;
}

/**
 * How the case reached its current state.
 *
 * Every transition here comes from the audit trail, so nothing is inferred: if
 * a step is missing from the trail it is missing here too. The current state is
 * marked distinctly from the history that produced it.
 */
export function LifecycleChain({ changes, currentState, loading }: Props) {
  if (loading) {
    return <p className="lifecycle__loading">Reading the audit trail…</p>;
  }
  if (changes.length === 0) {
    return (
      <p className="lifecycle__empty">
        The audit trail records no state transitions for this case.
      </p>
    );
  }

  return (
    <ol className="lifecycle">
      {changes.map((change, i) => {
        const isLast = i === changes.length - 1;
        const isCurrent = isLast && change.new_state === currentState;
        return (
          <li key={`${change.at}-${i}`} className={`lifecycle__step${isCurrent ? " is-current" : ""}`}>
            <div className="lifecycle__marker" aria-hidden="true" />
            <div className="lifecycle__content">
              <div className="lifecycle__states">
                <span className="lifecycle__from">{change.prev_state ?? "—"}</span>
                <span className="lifecycle__arrow" aria-hidden="true">→</span>
                {isCurrent ? (
                  <StatusBadge state={change.new_state ?? currentState} />
                ) : (
                  <span className="lifecycle__to">{change.new_state}</span>
                )}
              </div>
              <p className="lifecycle__meta">
                <span className="u-mono">{change.reason_code ?? "no reason recorded"}</span>
                {change.worker_id && <span> · {change.worker_id}</span>}
                <span className="lifecycle__time" title={absolute(change.at)}>
                  {relativeFromNow(change.at)}
                </span>
              </p>
            </div>
          </li>
        );
      })}
    </ol>
  );
}
