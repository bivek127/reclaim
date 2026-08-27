import { useEffect, useRef, useState } from "react";
import { humanizeState } from "@/lib/states";
import type { CaseFilters } from "@/hooks/useCaseFilters";
import "./FilterBar.css";

interface Props {
  filters: CaseFilters;
  availableStates: string[];
  attentionStates: string[];
  activeCount: number;
  resultTotal: number | undefined;
  onSearch: (q: string) => void;
  onToggleState: (state: string) => void;
  onToggleAttention: () => void;
  onTogglePendingReview: () => void;
  onClear: () => void;
}

/**
 * One coherent filter model rather than controls scattered across the page:
 * a search field, two quick toggles for the questions operators ask most, a
 * state picker, and a summary of what is currently applied.
 */
export function FilterBar({
  filters, availableStates, attentionStates, activeCount, resultTotal,
  onSearch, onToggleState, onToggleAttention, onTogglePendingReview, onClear,
}: Props) {
  const [draft, setDraft] = useState(filters.q);
  const [statesOpen, setStatesOpen] = useState(false);
  const popoverRef = useRef<HTMLDivElement>(null);

  // Keep the field in step when the URL changes underneath (back button, clear).
  useEffect(() => setDraft(filters.q), [filters.q]);

  // Debounced so typing does not issue a request per keystroke.
  useEffect(() => {
    if (draft === filters.q) return;
    const id = window.setTimeout(() => onSearch(draft), 300);
    return () => window.clearTimeout(id);
  }, [draft, filters.q, onSearch]);

  useEffect(() => {
    if (!statesOpen) return;
    function onDocClick(e: MouseEvent) {
      if (!popoverRef.current?.contains(e.target as Node)) setStatesOpen(false);
    }
    function onEscape(e: KeyboardEvent) {
      if (e.key === "Escape") setStatesOpen(false);
    }
    document.addEventListener("mousedown", onDocClick);
    document.addEventListener("keydown", onEscape);
    return () => {
      document.removeEventListener("mousedown", onDocClick);
      document.removeEventListener("keydown", onEscape);
    };
  }, [statesOpen]);

  return (
    <div className="filters">
      <div className="filters__row">
        <div className="filters__search">
          <label htmlFor="case-search" className="u-visually-hidden">
            Search cases by customer, order reference, provider reference, or case id
          </label>
          <input
            id="case-search"
            className="input"
            type="search"
            placeholder="Search customer, order, provider reference, or case id"
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") onSearch(draft); }}
          />
        </div>

        <button
          type="button"
          className={`chip${filters.needsAttention ? " chip--on chip--attention" : ""}`}
          aria-pressed={filters.needsAttention}
          onClick={onToggleAttention}
          title={`States needing a human: ${attentionStates.join(", ")}`}
        >
          Needs attention
        </button>

        <button
          type="button"
          className={`chip${filters.pendingReview ? " chip--on" : ""}`}
          aria-pressed={filters.pendingReview}
          onClick={onTogglePendingReview}
        >
          Pending review
        </button>

        <div className="filters__states" ref={popoverRef}>
          <button
            type="button"
            className={`chip${filters.states.length ? " chip--on" : ""}`}
            aria-expanded={statesOpen}
            aria-haspopup="true"
            onClick={() => setStatesOpen((v) => !v)}
          >
            State{filters.states.length ? ` · ${filters.states.length}` : ""}
            <span className="chip__caret" aria-hidden="true" />
          </button>

          {statesOpen && (
            <div className="popover" role="group" aria-label="Filter by case state">
              <ul className="popover__list">
                {availableStates.map((state) => {
                  const checked = filters.states.includes(state);
                  return (
                    <li key={state}>
                      <label className="popover__option">
                        <input
                          type="checkbox"
                          checked={checked}
                          onChange={() => onToggleState(state)}
                        />
                        <span>{humanizeState(state)}</span>
                        <span className="popover__code u-mono">{state}</span>
                      </label>
                    </li>
                  );
                })}
              </ul>
            </div>
          )}
        </div>

        {activeCount > 0 && (
          <button type="button" className="btn btn--ghost btn--sm" onClick={onClear}>
            Clear filters
          </button>
        )}

        <p className="filters__count" role="status">
          {resultTotal === undefined
            ? "Loading…"
            : `${resultTotal} ${resultTotal === 1 ? "case" : "cases"}`}
          {activeCount > 0 && resultTotal !== undefined ? " matching" : ""}
        </p>
      </div>

      {filters.states.length > 0 && (
        <ul className="filters__applied" aria-label="Applied state filters">
          {filters.states.map((state) => (
            <li key={state}>
              <button
                type="button"
                className="chip chip--applied"
                onClick={() => onToggleState(state)}
                aria-label={`Remove ${humanizeState(state)} filter`}
              >
                {humanizeState(state)}
                <span className="chip__x" aria-hidden="true">×</span>
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
