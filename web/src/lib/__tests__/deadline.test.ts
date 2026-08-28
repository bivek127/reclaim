import { describe, expect, it } from "vitest";
import { deadlineDistance } from "../time";

const NOW = Date.parse("2026-08-28T12:00:00Z");
const at = (ms: number) => new Date(NOW + ms).toISOString();

/** The function returns null only for a missing deadline, asserted separately. */
function distance(iso: string) {
  const d = deadlineDistance(iso, NOW);
  if (d === null) throw new Error(`expected a distance for ${iso}`);
  return d;
}
const MIN = 60_000;
const HOUR = 60 * MIN;

describe("deadlineDistance", () => {
  it("reads forward while the window is open", () => {
    expect(distance(at(41 * MIN))).toEqual({ text: "in 41m", expired: false });
  });

  it("reads backward once the window has closed", () => {
    expect(distance(at(-2 * HOUR))).toEqual({ text: "closed 2h ago", expired: true });
  });

  it("keeps minutes while the window is short enough to act on", () => {
    expect(distance(at(3 * HOUR + 20 * MIN)).text).toBe("in 3h 20m");
  });

  it("drops minutes once the distance is too long for them to matter", () => {
    // The long form is also what overflowed the queue's window column.
    expect(distance(at(-(13 * HOUR + 52 * MIN))).text).toBe("closed 13h ago");
    expect(distance(at(-(13 * HOUR + 52 * MIN))).text.length)
      .toBeLessThan("closed 13h 52m ago".length);
  });

  it("switches to days beyond twenty-four hours", () => {
    expect(distance(at(-(50 * HOUR))).text).toBe("closed 2d ago");
  });

  it("does not pretend to sub-minute precision", () => {
    expect(distance(at(-30_000))).toEqual({
      text: "closed under a minute ago", expired: true,
    });
  });

  it("treats the exact deadline as expired", () => {
    expect(distance(at(0)).expired).toBe(true);
  });

  it("returns null when there is no deadline, rather than inventing one", () => {
    expect(deadlineDistance(null, NOW)).toBeNull();
    expect(deadlineDistance(undefined, NOW)).toBeNull();
  });
});
