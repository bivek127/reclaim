import { useState } from "react";
import "./Identifier.css";

interface Props {
  value: string | null | undefined;
  /** Shorten long provider references; the full value stays copyable. */
  truncate?: boolean;
  label?: string;
}

/**
 * A technical identifier: provider reference, idempotency key, correlation id.
 *
 * Monospaced so character-level differences are visible, and copyable because
 * these are the values an investigator carries to the provider dashboard.
 */
export function Identifier({ value, truncate = false, label }: Props) {
  const [copied, setCopied] = useState(false);
  if (!value) return <span className="ident ident--empty">—</span>;

  const shown = truncate && value.length > 20 ? `${value.slice(0, 18)}…` : value;

  async function copy() {
    try {
      await navigator.clipboard.writeText(value!);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      // Clipboard unavailable; the full value is still selectable as text.
    }
  }

  return (
    <button
      type="button"
      className="ident"
      onClick={copy}
      title={label ? `${label}: ${value} (click to copy)` : `${value} (click to copy)`}
    >
      <span className="u-mono">{shown}</span>
      <span className="ident__hint" aria-hidden="true">{copied ? "copied" : ""}</span>
      <span className="u-visually-hidden">
        {label ? `${label} ${value}` : value}. Activate to copy.
      </span>
    </button>
  );
}
