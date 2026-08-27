import { STATE_PRESENTATION, isTerminal, presentationFor } from "@/lib/states";
import type { Meta } from "@/lib/types";
import "./StateVocabulary.css";

/**
 * The state vocabulary the backend actually reports.
 *
 * The list and the groupings come from `/api/meta`, which reads them from the
 * domain — the console does not keep its own copy of which states need a human.
 * Any state the API reports that this console has no description for is called
 * out rather than silently rendered as an unlabelled row, because that gap is
 * a genuine drift signal between the deployed domain and this build.
 */
export function StateVocabulary({ meta }: { meta: Meta }) {
  const attention = new Set(meta.attention_states);
  const inFlight = new Set(meta.in_flight_states);
  const undescribed = meta.case_states.filter((s) => !(s in STATE_PRESENTATION));

  const group = (state: string) => {
    if (attention.has(state)) return "Needs a human";
    if (inFlight.has(state)) return "In flight";
    if (isTerminal(state)) return "Terminal";
    return "Processing";
  };

  const order = ["Needs a human", "In flight", "Processing", "Terminal"];
  const grouped = order
    .map((name) => ({ name, states: meta.case_states.filter((s) => group(s) === name) }))
    .filter((g) => g.states.length > 0);

  return (
    <div className="voc">
      {grouped.map((g) => (
        <div className="voc__group" key={g.name}>
          <p className="u-label voc__group-name">
            {g.name} <span className="voc__count">{g.states.length}</span>
          </p>
          <ul>
            {g.states.map((state) => (
              <li className="voc__row" key={state}>
                <code className={`voc__code voc__code--${presentationFor(state).semantic}`}>
                  {state}
                </code>
                <span className="voc__meaning">{presentationFor(state).meaning}</span>
              </li>
            ))}
          </ul>
        </div>
      ))}

      {undescribed.length > 0 && (
        <p className="voc__drift" role="status">
          {undescribed.length} state{undescribed.length === 1 ? "" : "s"} reported by
          the API {undescribed.length === 1 ? "has" : "have"} no description in this
          console: <span className="u-mono">{undescribed.join(", ")}</span>. It is
          running a newer domain than this build knows about.
        </p>
      )}
    </div>
  );
}
