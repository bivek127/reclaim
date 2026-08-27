import { formatMoney, describeMoney } from "@/lib/money";
import "./Money.css";

interface Props {
  minorUnits: number;
  currency: string;
  /** `hero` for the figure a page is about; `inline` inside dense rows. */
  size?: "hero" | "strong" | "inline";
  /** Show the ISO code beside the amount. */
  showCode?: boolean;
}

/**
 * A monetary amount.
 *
 * Amount and currency always render together — an amount alone is not a
 * financial fact. Formatting happens here and only here; the value itself is
 * the integer the backend stored.
 */
export function Money({ minorUnits, currency, size = "inline", showCode = false }: Props) {
  return (
    <span className={`money money--${size}`}>
      <span aria-hidden="true">{formatMoney(minorUnits, currency)}</span>
      {showCode && <span className="money__code" aria-hidden="true">{currency}</span>}
      <span className="u-visually-hidden">{describeMoney(minorUnits, currency)}</span>
    </span>
  );
}
