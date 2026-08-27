import { Link } from "react-router-dom";
import type { SystemStatus } from "@/lib/types";
import "./SafetySignals.css";

interface Signal {
  label: string;
  value: number;
  /** What the number means operationally, in the reader's own terms. */
  meaning: string;
  /** Raised only when the value itself is the thing worth looking at. */
  notable: boolean;
  to?: string;
  toLabel?: string;
}

/**
 * Concurrency and safety counters.
 *
 * A rejected stale write is the fencing protection doing its job, so it is
 * never styled as a failure — an operator who learns to fear it will start
 * asking for the guard to be removed. Held leases are ordinary machinery and
 * stay flat no matter how many there are. Only counters whose non-zero value
 * genuinely warrants a look are raised, and each one says what it means rather
 * than leaving a bare integer to be interpreted.
 */
export function SafetySignals({ system }: { system: SystemStatus }) {
  const signals: Signal[] = [
    {
      label: "Leases held",
      value: system.leases_held,
      meaning: "Cases a worker currently holds. Routine while work is in progress.",
      notable: false,
    },
    {
      label: "Leases expired",
      value: system.leases_expired,
      meaning:
        system.leases_expired > 0
          ? "A worker stopped without releasing its case. The sweeper reclaims these; a number that stays high means workers are dying."
          : "No case is held by a worker that stopped without releasing it.",
      notable: system.leases_expired > 0,
    },
    {
      label: "Stale writes rejected",
      value: system.stale_writes_rejected,
      meaning:
        system.stale_writes_rejected > 0
          ? "Fencing refused a write from a worker holding an outdated token. The protection worked; the case was not corrupted."
          : "No worker has attempted a write with an outdated fencing token.",
      notable: false,
    },
    {
      label: "Open actions",
      value: system.open_actions,
      meaning: "Actions proposed, live, or unresolved. At most one per case is permitted.",
      notable: false,
    },
    {
      label: "Unresolved attempts",
      value: system.unresolved_attempts,
      meaning:
        system.unresolved_attempts > 0
          ? "Execution attempts still prepared, in flight, or of unknown outcome. Reconciliation decides these; money may have moved."
          : "Every execution attempt has reached a known outcome.",
      notable: system.unresolved_attempts > 0,
      to: "/cases?state=AMBIGUOUS&state=RECONCILING",
      toLabel: "Review unresolved cases",
    },
  ];

  return (
    <ul className="sig" aria-label="Concurrency and safety counters">
      {signals.map((s) => (
        <li className={`sig__row${s.notable ? " is-notable" : ""}`} key={s.label}>
          <div className="sig__top">
            <span className="sig__label">{s.label}</span>
            <span className="sig__value">{s.value}</span>
          </div>
          <p className="sig__meaning">{s.meaning}</p>
          {s.notable && s.to && s.value > 0 && (
            <Link className="sig__link" to={s.to}>{s.toLabel}</Link>
          )}
        </li>
      ))}
    </ul>
  );
}
