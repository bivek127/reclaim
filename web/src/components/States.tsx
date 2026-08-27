import type { ReactNode } from "react";
import "./States.css";

/**
 * Loading, empty, and error presentations.
 *
 * Loading mirrors the shape of the content it replaces rather than spinning,
 * so the page does not reflow when data lands. Errors say what failed, whether
 * what is on screen may be stale, and what to do next — never a stack trace.
 */

export function SkeletonRows({ rows = 6, columns = 5 }: { rows?: number; columns?: number }) {
  return (
    <div className="skeleton" aria-busy="true" aria-live="polite">
      <span className="u-visually-hidden">Loading…</span>
      {Array.from({ length: rows }).map((_, r) => (
        <div className="skeleton__row" key={r}>
          {Array.from({ length: columns }).map((__, c) => (
            <div className="skeleton__cell" key={c} />
          ))}
        </div>
      ))}
    </div>
  );
}

export function SkeletonBlock({ height = 120 }: { height?: number }) {
  return (
    <div className="skeleton__block" style={{ height }} aria-busy="true">
      <span className="u-visually-hidden">Loading…</span>
    </div>
  );
}

interface EmptyProps {
  title: string;
  description?: string;
  action?: ReactNode;
}

export function EmptyState({ title, description, action }: EmptyProps) {
  return (
    <div className="state state--empty">
      <p className="state__title">{title}</p>
      {description && <p className="state__body">{description}</p>}
      {action && <div className="state__action">{action}</div>}
    </div>
  );
}

interface ErrorProps {
  title?: string;
  error: unknown;
  onRetry?: () => void;
  /** True when stale content remains on screen behind this message. */
  stale?: boolean;
}

export function ErrorState({ title, error, onRetry, stale = false }: ErrorProps) {
  const message =
    error instanceof Error ? error.message : "An unexpected problem occurred.";
  return (
    <div className="state state--error" role="alert">
      <p className="state__title">{title ?? "Could not load this view"}</p>
      <p className="state__body">{message}</p>
      {stale && (
        <p className="state__note">
          Anything still on screen was loaded earlier and may no longer be current.
        </p>
      )}
      {onRetry && (
        <div className="state__action">
          <button type="button" className="btn btn--secondary" onClick={onRetry}>
            Try again
          </button>
        </div>
      )}
    </div>
  );
}
