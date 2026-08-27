import { presentationFor, humanizeState } from "@/lib/states";
import "./StatusBadge.css";

interface Props {
  state: string;
  /** `full` shows the humanised label; `code` shows the literal state name. */
  variant?: "full" | "code";
  title?: string;
}

/**
 * Case state, shown as tint + shape + literal name.
 *
 * The state name is always present as text, so the badge never depends on
 * colour alone to communicate meaning.
 */
export function StatusBadge({ state, variant = "full", title }: Props) {
  const { semantic, meaning, actionable } = presentationFor(state);
  const label = variant === "code" ? state : humanizeState(state);
  return (
    <span
      className={`badge badge--${semantic}`}
      title={title ?? meaning}
      data-actionable={actionable ? "true" : undefined}
    >
      <span className="badge__marker" aria-hidden="true" />
      <span className="badge__label">{label}</span>
    </span>
  );
}
