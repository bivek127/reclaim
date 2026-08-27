import { describe, expect, it } from "vitest";
import { CASE_PARAM, casesPath, casePath, caseTimelinePath, reviewPath } from "../routes";
import { renderHook } from "@testing-library/react";
import { MemoryRouter, useSearchParams } from "react-router-dom";
import { createElement, type ReactNode } from "react";

describe("route builders", () => {
  it("builds the attention filter with the parameter the queue actually reads", () => {
    expect(casesPath({ attention: true })).toBe("/cases?attention=1");
  });

  it("repeats the state parameter so multiple states survive", () => {
    expect(casesPath({ states: ["AMBIGUOUS", "RECONCILING"] }))
      .toBe("/cases?state=AMBIGUOUS&state=RECONCILING");
  });

  it("omits empty filters rather than emitting blank parameters", () => {
    expect(casesPath({ query: "   ", states: [] })).toBe("/cases");
  });

  it("addresses cases and reviews by the same id", () => {
    expect(casePath(6)).toBe("/cases/6");
    expect(caseTimelinePath(6)).toBe("/cases/6/timeline");
    expect(reviewPath(6)).toBe("/reviews/6");
  });
});

/**
 * The builders and the queue must agree on parameter names. A link whose
 * parameter the queue does not recognise is silently ignored, so this reads a
 * built URL back through the router the way the page does.
 */
describe("built links round-trip through the queue's own parser", () => {
  function readBack(url: string) {
    const wrapper = ({ children }: { children: ReactNode }) =>
      createElement(MemoryRouter, { initialEntries: [url] }, children);
    return renderHook(() => useSearchParams(), { wrapper }).result;
  }

  it("round-trips the attention filter", () => {
    const { current } = readBack(casesPath({ attention: true }));
    expect(current[0].get(CASE_PARAM.attention)).toBe("1");
  });

  it("round-trips every state in a multi-state filter", () => {
    const { current } = readBack(casesPath({ states: ["HALTED", "ESCALATED"] }));
    expect(current[0].getAll(CASE_PARAM.state)).toEqual(["HALTED", "ESCALATED"]);
  });

  it("round-trips a search term", () => {
    const { current } = readBack(casesPath({ query: "ord_4106" }));
    expect(current[0].get(CASE_PARAM.query)).toBe("ord_4106");
  });
});
