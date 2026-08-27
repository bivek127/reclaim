import { useNavigate } from "react-router-dom";
import { Money } from "./Money";
import { StatusBadge } from "./StatusBadge";
import { RelativeTime } from "./RelativeTime";
import { presentationFor } from "@/lib/states";
import { deadlineDistance, absolute } from "@/lib/time";
import type { CaseRow } from "@/lib/types";
import type { SortValue } from "@/hooks/useCaseFilters";
import "./CasesTable.css";

interface Props {
  rows: CaseRow[];
  sort: SortValue;
  direction: "asc" | "desc";
  onSort: (sort: SortValue) => void;
}

/**
 * The queue table.
 *
 * Columns answer, in order: what is this, whose is it, how much is at stake,
 * what state is it in, how much budget remains, is a window closing, and when
 * did it last move. Amounts are right-aligned so magnitudes compare at a
 * glance; identifiers are monospaced so near-identical references are
 * distinguishable.
 */
export function CasesTable({ rows, sort, direction, onSort }: Props) {
  const navigate = useNavigate();
  const arrow = direction === "asc" ? "▲" : "▼";

  function open(caseId: number) {
    navigate(`/cases/${caseId}`);
  }

  function sortable(key: SortValue, label: string, numeric = false) {
    const active = sort === key;
    const ariaSort = active ? (direction === "asc" ? "ascending" : "descending") : "none";
    return (
      <th scope="col" className={numeric ? "is-numeric" : undefined} aria-sort={ariaSort}>
        <button type="button" className="ctable__sort" onClick={() => onSort(key)} aria-sort={ariaSort}>
          {label}
          {active && <span className="ctable__arrow" aria-hidden="true">{arrow}</span>}
        </button>
      </th>
    );
  }

  return (
    <div className="table-wrap">
      <table className="ctable">
        <caption className="u-visually-hidden">
          Recovery cases. Activate a row to open the case.
        </caption>
        <colgroup>
          <col className="col-case" />
          <col className="col-customer col-optional" />
          <col className="col-amount" />
          <col className="col-state" />
          <col className="col-attempts col-optional" />
          <col className="col-window col-optional" />
          <col className="col-updated" />
        </colgroup>
        <thead>
          <tr>
            <th scope="col">Case</th>
            <th scope="col" className="col-optional">Customer</th>
            {sortable("amount", "Amount", true)}
            <th scope="col">State</th>
            <th scope="col" className="is-numeric col-optional">Attempts</th>
            <th scope="col" className="col-optional">Window</th>
            {sortable("updated_at", "Updated")}
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const { actionable } = presentationFor(row.state);
            const needsHuman = actionable || row.has_pending_review;
            const window = deadlineDistance(row.action_deadline_at);
            // A closed window only demands action while the case is still waiting.
            const waiting = row.state === "AWAITING_CUSTOMER";
            const windowClass = !window
              ? ""
              : window.expired
                ? waiting ? "is-urgent" : "is-passed"
                : "is-open";
            const budgetSpent = row.attempt_count >= row.max_attempts;

            return (
              <tr
                key={row.case_id}
                tabIndex={0}
                role="link"
                aria-label={`Case ${row.case_id}, ${row.state}`}
                className={needsHuman ? "is-attention" : undefined}
                onClick={() => open(row.case_id)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" || e.key === " ") {
                    e.preventDefault();
                    open(row.case_id);
                  }
                }}
              >
                <td>
                  <span className="cell-case">
                    <span className="cell-case__id">#{row.case_id}</span>
                    <span className="cell-case__anchor">{row.anchor_key}</span>
                  </span>
                </td>
                <td className="cell-customer col-optional">{row.customer_ref}</td>
                <td className="is-numeric">
                  <Money minorUnits={row.amount_minor} currency={row.currency} />
                </td>
                <td>
                  <span className="cell-state">
                    <StatusBadge state={row.state} />
                    {row.has_pending_review && (
                      <span className="review-flag">Review</span>
                    )}
                  </span>
                </td>
                <td className={`is-numeric cell-attempts col-optional${budgetSpent ? " is-spent" : ""}`}>
                  {row.attempt_count}/{row.max_attempts}
                </td>
                <td className={`cell-window col-optional ${windowClass}`}>
                  {window ? (
                    <span title={absolute(row.action_deadline_at)}>{window.text}</span>
                  ) : (
                    <span className="cell-window is-passed">—</span>
                  )}
                </td>
                <td>
                  <RelativeTime value={row.updated_at} className="cell-time" />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
