import type { SystemStatus } from "@/lib/types";
import { absolute, relativeFromNow } from "@/lib/time";
import "./BreakerPanel.css";

interface Props {
  breaker: SystemStatus["breaker"];
  /** True when the status query itself failed; state is then unknown, not CLOSED. */
  indeterminate?: boolean;
}

/**
 * The dispatch gate.
 *
 * The breaker has exactly two stored states, CLOSED and OPEN. There is no
 * half-open probe, so none is drawn. An open breaker stops provider dispatch
 * and nothing else: ingestion, diagnosis, policy, reconciliation, verification
 * and human review all continue to run, which is why this never reads as an
 * outage banner.
 *
 * Nothing in the running system closes the breaker. State changes belong to a
 * monitor job that is not built yet, so `reset_after` is the earliest time a
 * reset would be due rather than a promise that one will happen. Presenting it
 * as a countdown to recovery would tell an operator to wait for an event that
 * cannot occur.
 */
export function BreakerPanel({ breaker, indeterminate = false }: Props) {
  if (indeterminate || breaker === null) {
    return (
      <section className="brk brk--unknown" aria-labelledby="brk-h">
        <div className="brk__head">
          <span className="brk__mark brk__mark--unknown" aria-hidden="true" />
          <div>
            <p className="u-label">Circuit breaker</p>
            <h2 className="brk__state" id="brk-h">Unknown</h2>
          </div>
        </div>
        <p className="brk__meaning">
          The breaker's state could not be read. Dispatch may or may not be
          running — this is not the same as a closed breaker.
        </p>
      </section>
    );
  }

  const open = breaker.state === "OPEN";
  const cause = formatCause(breaker.trip_cause);

  return (
    <section className={`brk ${open ? "brk--open" : "brk--closed"}`} aria-labelledby="brk-h">
      <div className="brk__head">
        <span className={`brk__mark brk__mark--${open ? "open" : "closed"}`} aria-hidden="true" />
        <div>
          <p className="u-label">Circuit breaker</p>
          <h2 className="brk__state" id="brk-h">{breaker.state}</h2>
        </div>
        <p className="brk__meaning">
          {open
            ? "Dispatch to the provider is stopped. Cases that reach the executor halt instead of being sent."
            : "Dispatch to the provider is permitted."}
        </p>
      </div>

      {open && (
        <>
          <p className="brk__scope">
            Recovery has not stopped. Ingestion, diagnosis, policy evaluation,
            reconciliation, verification and human review continue to run.
          </p>
          <dl className="brk__facts">
            {breaker.opened_at && (
              <div>
                <dt>Opened</dt>
                <dd title={absolute(breaker.opened_at)}>{relativeFromNow(breaker.opened_at)}</dd>
              </div>
            )}
            <div>
              <dt>Consecutive failures</dt>
              <dd>{breaker.consecutive_failures}</dd>
            </div>
            {cause && (
              <div className="brk__facts-wide">
                <dt>Trip cause</dt>
                <dd className="u-mono">{cause}</dd>
              </div>
            )}
            {breaker.reset_after && (
              <div className="brk__facts-wide">
                <dt>Reset due after</dt>
                <dd title={absolute(breaker.reset_after)}>
                  {absolute(breaker.reset_after)}
                  <span className="brk__caveat">
                    {" "}— the earliest a reset is due. The breaker does not close
                    on its own; a monitor job has to close it.
                  </span>
                </dd>
              </div>
            )}
          </dl>
        </>
      )}

      {!open && breaker.consecutive_failures > 0 && (
        <p className="brk__scope">
          {breaker.consecutive_failures} consecutive execution{" "}
          {breaker.consecutive_failures === 1 ? "failure has" : "failures have"} been
          counted since the last accepted dispatch. The gate is still open for traffic.
        </p>
      )}
    </section>
  );
}

/** Renders the stored trip cause without asserting a shape the column does not have. */
function formatCause(cause: unknown): string | null {
  if (cause === null || cause === undefined) return null;
  if (typeof cause === "string") return cause;
  try {
    const text = JSON.stringify(cause);
    return text === "{}" ? null : text;
  } catch {
    return null;
  }
}
