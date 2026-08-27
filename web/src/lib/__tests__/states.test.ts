import { describe, expect, it } from "vitest";
import { STATE_PRESENTATION, presentationFor, humanizeState } from "../states";

// The authoritative list, mirrored from the database enum. If the domain adds
// a state, this test is where the omission surfaces.
const DOMAIN_STATES = [
  "NEW", "ENRICHING", "DIAGNOSING", "POLICY_EVAL", "ACTION_READY", "EXECUTING",
  "AWAITING_CUSTOMER", "ATTEMPT_FAILED", "AMBIGUOUS", "RECONCILING", "HALTED",
  "ESCALATED", "VERIFIED_RECOVERED", "VERIFIED_FAILED", "EXPIRED_UNRESOLVED",
];

describe("case state presentation", () => {
  it("covers every state the domain can produce", () => {
    for (const state of DOMAIN_STATES) {
      expect(STATE_PRESENTATION[state], `missing ${state}`).toBeDefined();
    }
  });

  it("invents no states beyond the domain vocabulary", () => {
    expect(Object.keys(STATE_PRESENTATION).sort()).toEqual([...DOMAIN_STATES].sort());
  });

  it("treats an unknown outcome as needing attention, not as routine", () => {
    // Unknown money state is an operational emergency.
    expect(presentationFor("AMBIGUOUS").semantic).toBe("attention");
    expect(presentationFor("AMBIGUOUS").actionable).toBe(true);
  });

  it("does not colour a confirmed non-recovery as an error", () => {
    // VERIFIED_FAILED is a legitimate resolved outcome, not a system failure.
    expect(presentationFor("VERIFIED_FAILED").semantic).toBe("neutral");
  });

  it("marks only the states a human must act on as actionable", () => {
    const actionable = DOMAIN_STATES.filter((s) => presentationFor(s).actionable);
    expect(actionable.sort()).toEqual(
      ["AMBIGUOUS", "ESCALATED", "EXPIRED_UNRESOLVED", "HALTED"].sort(),
    );
  });

  it("reserves success for independently verified recovery", () => {
    const success = DOMAIN_STATES.filter((s) => presentationFor(s).semantic === "success");
    expect(success).toEqual(["VERIFIED_RECOVERED"]);
  });

  it("falls back safely for a state it has never seen", () => {
    expect(presentationFor("SOMETHING_NEW").semantic).toBe("neutral");
  });

  it("humanises state names for display", () => {
    expect(humanizeState("AWAITING_CUSTOMER")).toBe("Awaiting Customer");
    expect(humanizeState("VERIFIED_RECOVERED")).toBe("Verified Recovered");
  });
});
