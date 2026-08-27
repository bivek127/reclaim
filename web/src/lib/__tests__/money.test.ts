import { describe, expect, it } from "vitest";
import { formatMoney, describeMoney, exponentFor } from "../money";

describe("money formatting", () => {
  it("renders minor units as major units with the currency symbol", () => {
    expect(formatMoney(425000, "INR")).toBe("₹4,250.00");
    expect(formatMoney(129900, "INR")).toBe("₹1,299.00");
  });

  it("keeps trailing zeroes in the fractional part", () => {
    // 4250.5 must never render as "4,250.5"
    expect(formatMoney(425050, "INR")).toBe("₹4,250.50");
    expect(formatMoney(425005, "INR")).toBe("₹4,250.05");
  });

  it("uses Indian digit grouping for INR", () => {
    expect(formatMoney(1650000, "INR")).toBe("₹16,500.00");
    expect(formatMoney(12345678900, "INR")).toBe("₹12,34,56,789.00");
  });

  it("uses western grouping for other currencies", () => {
    expect(formatMoney(123456789, "USD")).toBe("$1,234,567.89");
  });

  it("respects currencies with no minor unit", () => {
    expect(exponentFor("JPY")).toBe(0);
    expect(formatMoney(5100, "JPY")).toBe("¥5,100");
  });

  it("handles zero and small amounts exactly", () => {
    expect(formatMoney(0, "INR")).toBe("₹0.00");
    expect(formatMoney(1, "INR")).toBe("₹0.01");
    expect(formatMoney(99, "INR")).toBe("₹0.99");
  });

  it("does not lose precision on values where float division drifts", () => {
    // 0.1 + 0.2 style drift must be impossible: these are integer inputs.
    expect(formatMoney(2260, "USD")).toBe("$22.60");
    expect(formatMoney(70, "USD")).toBe("$0.70");
    expect(formatMoney(1010, "USD")).toBe("$10.10");
  });

  it("formats large values without scientific notation or rounding", () => {
    expect(formatMoney(999999999999, "USD")).toBe("$9,999,999,999.99");
  });

  it("renders negatives with the sign outside the symbol", () => {
    expect(formatMoney(-425000, "INR")).toBe("-₹4,250.00");
  });

  it("describes amounts unambiguously for assistive technology", () => {
    expect(describeMoney(425000, "INR")).toBe("4250.00 INR");
    expect(describeMoney(5100, "JPY")).toBe("5100 JPY");
  });

  it("defaults unknown currencies to two minor digits rather than guessing", () => {
    expect(exponentFor("XYZ")).toBe(2);
    expect(formatMoney(1234, "XYZ")).toBe("12.34");
  });
});
