import { describe, expect, it } from "vitest";
import { absolute, compact } from "../time";

/**
 * Every clock in the console reads the same way. A tooltip in 12-hour time
 * beside a timeline row in 24-hour time makes the reader convert before they
 * can compare two instants, which is exactly what forensic reading cannot
 * afford.
 */
describe("clock convention", () => {
  const evening = "2026-08-27T21:59:15.000+05:30";

  it("renders the full instant on a 24-hour clock", () => {
    const text = absolute(evening);
    expect(text).not.toMatch(/\b[AP]M\b/i);
    expect(text).toMatch(/21:59:15/);
  });

  it("uses the same convention in the compact form", () => {
    expect(compact(evening)).not.toMatch(/\b[AP]M\b/i);
    expect(compact(evening)).toMatch(/21:59/);
  });

  it("renders a missing instant as an em dash rather than a fabricated time", () => {
    expect(absolute(null)).toBe("—");
    expect(compact(undefined)).toBe("—");
  });
});
