/**
 * Case-state presentation.
 *
 * The vocabulary itself comes from the API (`/api/meta`), which reads it from
 * the domain. This module only says how a state should *look* and what it
 * means for an operator; it never invents a state or decides a transition.
 */

export type Semantic =
  | "success" | "attention" | "danger" | "progress" | "waiting" | "neutral";

interface StatePresentation {
  semantic: Semantic;
  /** What this state means for the operator, in one line. */
  meaning: string;
  /** True when a human is expected to act. */
  actionable?: boolean;
}

export const STATE_PRESENTATION: Record<string, StatePresentation> = {
  NEW:                { semantic: "neutral",  meaning: "Case opened, not yet enriched." },
  ENRICHING:          { semantic: "neutral",  meaning: "Gathering context for diagnosis." },
  DIAGNOSING:         { semantic: "neutral",  meaning: "Classifying the failure cause." },
  POLICY_EVAL:        { semantic: "neutral",  meaning: "Deciding what action is permitted." },
  ACTION_READY:       { semantic: "neutral",  meaning: "An action is approved and queued for dispatch." },
  EXECUTING:          { semantic: "progress", meaning: "Dispatching to the provider." },
  AWAITING_CUSTOMER:  { semantic: "waiting",  meaning: "Payment link is live; waiting on the customer." },
  ATTEMPT_FAILED:     { semantic: "neutral",  meaning: "This attempt failed; the case may try another action." },
  // Unknown money state is an operational emergency, not a quiet grey row.
  AMBIGUOUS:          { semantic: "attention", actionable: true,
                        meaning: "The provider outcome is unknown. Money may or may not have moved." },
  RECONCILING:        { semantic: "progress", meaning: "Querying the provider to resolve the unknown outcome." },
  HALTED:             { semantic: "danger",   actionable: true,
                        meaning: "Dispatch stopped by the circuit breaker." },
  ESCALATED:          { semantic: "attention", actionable: true,
                        meaning: "Handed to a human for a decision." },
  VERIFIED_RECOVERED: { semantic: "success",  meaning: "Payment independently verified. Revenue recognised." },
  // A legitimate resolved outcome, not a system error — so not red.
  VERIFIED_FAILED:    { semantic: "neutral",  meaning: "Confirmed not recovered. Case closed." },
  EXPIRED_UNRESOLVED: { semantic: "attention", actionable: true,
                        meaning: "Time ran out before the outcome could be resolved." },
};

export function presentationFor(state: string): StatePresentation {
  return (
    STATE_PRESENTATION[state] ?? {
      semantic: "neutral",
      meaning: "Unrecognised state.",
    }
  );
}

/** Human-facing label. The literal state name is always shown alongside. */
export function humanizeState(state: string): string {
  return state
    .toLowerCase()
    .split("_")
    .map((w) => w.charAt(0).toUpperCase() + w.slice(1))
    .join(" ");
}
