/**
 * Money formatting at the presentation boundary.
 *
 * The backend stores integer minor units and that is what crosses the wire.
 * Nothing here divides by a power of ten in floating point: the whole and
 * fractional parts are separated with integer arithmetic, then reassembled as
 * text. A value is never re-derived, converted, or summed in the browser, and
 * currency is never inferred — an amount without its currency is not rendered.
 */

// Minor-unit exponents for currencies this system can encounter. Anything
// unlisted falls back to two, which is correct for INR and every currency the
// schema currently allows.
const EXPONENT: Record<string, number> = { INR: 2, USD: 2, EUR: 2, GBP: 2, JPY: 0 };

const SYMBOL: Record<string, string> = { INR: "₹", USD: "$", EUR: "€", GBP: "£", JPY: "¥" };

// Indian digit grouping for INR (1,23,456.78); Western grouping elsewhere.
const LOCALE: Record<string, string> = { INR: "en-IN" };

export function exponentFor(currency: string): number {
  return EXPONENT[currency.toUpperCase()] ?? 2;
}

export function symbolFor(currency: string): string {
  return SYMBOL[currency.toUpperCase()] ?? "";
}

/**
 * Split integer minor units into sign, whole units, and fractional remainder
 * without leaving integer arithmetic.
 */
function split(minor: number, exponent: number) {
  const negative = minor < 0;
  const magnitude = Math.abs(Math.trunc(minor));
  if (exponent === 0) {
    return { negative, whole: magnitude, fraction: "" };
  }
  const divisor = 10 ** exponent; // exact for the exponents we support
  const whole = Math.trunc(magnitude / divisor);
  const remainder = magnitude - whole * divisor;
  return { negative, whole, fraction: String(remainder).padStart(exponent, "0") };
}

/** `425000, "INR"` -> `"₹4,250.00"`. Amount and currency always travel together. */
export function formatMoney(minorUnits: number, currency: string): string {
  const code = currency.toUpperCase();
  const exponent = exponentFor(code);
  const { negative, whole, fraction } = split(minorUnits, exponent);
  const grouped = new Intl.NumberFormat(LOCALE[code] ?? "en-US", {
    useGrouping: true,
    maximumFractionDigits: 0,
  }).format(whole);
  const body = fraction ? `${grouped}.${fraction}` : grouped;
  return `${negative ? "-" : ""}${symbolFor(code)}${body}`;
}

/** Screen-reader and tooltip form: unambiguous about the currency. */
export function describeMoney(minorUnits: number, currency: string): string {
  const code = currency.toUpperCase();
  const exponent = exponentFor(code);
  const { negative, whole, fraction } = split(minorUnits, exponent);
  const body = fraction ? `${whole}.${fraction}` : String(whole);
  return `${negative ? "minus " : ""}${body} ${code}`;
}
