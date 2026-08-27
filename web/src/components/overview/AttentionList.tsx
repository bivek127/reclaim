import { Link, useNavigate } from "react-router-dom";
import { Money } from "../Money";
import { StatusBadge } from "../StatusBadge";
import { EmptyState } from "../States";
import { presentationFor } from "@/lib/states";
import { absolute, deadlineDistance, relativeFromNow } from "@/lib/time";
import type { CaseRow } from "@/lib/types";
import "./AttentionList.css";
import { casesPath } from "@/lib/routes";

interface Props {
  rows: CaseRow[];
  total: number;
}

/**
 * Cases a human needs to look at, highest value first.
 *
 * Deliberately not a second copy of the case queue: a short triage list with
 * just enough context to decide whether to open something now. "Attention" is
 * whatever the API marks as needing attention — this list never applies its
 * own definition.
 */
export function AttentionList({ rows, total }: Props) {
  const navigate = useNavigate();

  if (rows.length === 0) {
    return (
      <EmptyState
        title="Nothing needs attention"
        description="No case is escalated, ambiguous, or halted. Cases in flight are progressing on their own."
      />
    );
  }

  return (
    <>
      <ul className="attn">
        {rows.map((row) => {
          const { meaning } = presentationFor(row.state);
          const window = deadlineDistance(row.action_deadline_at);
          return (
            <li
              key={row.case_id}
              className="attn__row"
              tabIndex={0}
              role="link"
              aria-label={`Case ${row.case_id}, ${row.state}`}
              onClick={() => navigate(`/cases/${row.case_id}`)}
              onKeyDown={(e) => {
                if (e.key === "Enter" || e.key === " ") {
                  e.preventDefault();
                  navigate(`/cases/${row.case_id}`);
                }
              }}
            >
              <div className="attn__lead">
                <span className="attn__id">#{row.case_id}</span>
                <span className="attn__anchor u-mono">{row.anchor_key}</span>
              </div>

              <div className="attn__mid">
                <div className="attn__states">
                  <StatusBadge state={row.state} />
                  {row.has_pending_review && (
                    <span className="attn__review">Awaiting review</span>
                  )}
                </div>
                <p className="attn__why">{meaning}</p>
              </div>

              <div className="attn__tail">
                <Money minorUnits={row.amount_minor} currency={row.currency} size="strong" />
                <p className="attn__time" title={absolute(row.updated_at)}>
                  {window && !window.expired
                    ? `window ${window.text}`
                    : `updated ${relativeFromNow(row.updated_at)}`}
                </p>
              </div>
            </li>
          );
        })}
      </ul>

      {total > rows.length && (
        <p className="attn__more">
          <Link to={casesPath({ attention: true })}>
            {total - rows.length} more needing attention
          </Link>
        </p>
      )}
    </>
  );
}
