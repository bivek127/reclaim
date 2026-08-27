import { Link } from "react-router-dom";
import { humanizeState, presentationFor } from "@/lib/states";
import "./StateDistribution.css";

interface Props {
  counts: Record<string, number>;
  /** The full domain vocabulary, so a zero state is absent rather than unknown. */
  order: string[];
}

/**
 * Where the estate currently sits.
 *
 * A dense ranked list rather than a chart: an operator wants to read counts and
 * click through, not estimate wedge areas. States with no cases are summarised
 * at the end rather than padding the list with zeroes.
 *
 * States are never merged. AMBIGUOUS (money state unknown) and VERIFIED_FAILED
 * (a resolved, legitimate non-recovery) mean materially different things.
 */
export function StateDistribution({ counts, order }: Props) {
  const present = order
    .filter((state) => (counts[state] ?? 0) > 0)
    .sort((a, b) => (counts[b] ?? 0) - (counts[a] ?? 0));
  const absent = order.filter((state) => (counts[state] ?? 0) === 0);
  const max = Math.max(1, ...present.map((s) => counts[s] ?? 0));

  if (present.length === 0) {
    return <p className="dist__empty">No cases exist yet.</p>;
  }

  return (
    <>
      <ul className="dist" aria-label="Cases by state">
        {present.map((state) => {
          const count = counts[state] ?? 0;
          const { semantic } = presentationFor(state);
          return (
            <li key={state}>
              <Link className="dist__row" to={`/cases?state=${state}`}>
                <span className="dist__name">
                  {humanizeState(state)}
                  <span className="dist__code u-mono">{state}</span>
                </span>
                <span className="dist__bar" aria-hidden="true">
                  <span
                    className={`dist__fill dist__fill--${semantic}`}
                    style={{ width: `${Math.round((count / max) * 100)}%` }}
                  />
                </span>
                <span className="dist__count">{count}</span>
              </Link>
            </li>
          );
        })}
      </ul>
      {absent.length > 0 && (
        <p className="dist__absent">
          {absent.length} of {order.length} states have no cases.
        </p>
      )}
    </>
  );
}
