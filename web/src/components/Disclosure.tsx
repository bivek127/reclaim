import { useId, useState } from "react";
import type { ReactNode } from "react";
import "./Disclosure.css";

interface Props {
  label: string;
  children: ReactNode;
  defaultOpen?: boolean;
  count?: number;
}

/**
 * Progressive disclosure for technical evidence.
 *
 * Summary stays visible; identifiers, payloads, and versions sit one
 * deliberate click away so an operations user is not made to read what only a
 * technical investigator needs.
 */
export function Disclosure({ label, children, defaultOpen = false, count }: Props) {
  const [open, setOpen] = useState(defaultOpen);
  const id = useId();
  return (
    <div className="disclosure">
      <button
        type="button"
        className="disclosure__toggle"
        aria-expanded={open}
        aria-controls={id}
        onClick={() => setOpen((v) => !v)}
      >
        <span className={`disclosure__caret${open ? " is-open" : ""}`} aria-hidden="true" />
        {label}
        {count !== undefined && <span className="disclosure__count">{count}</span>}
      </button>
      <div id={id} hidden={!open} className="disclosure__body">
        {open && children}
      </div>
    </div>
  );
}

/** Key/value rows for technical payloads. Keys stay narrow so values align. */
export function TechnicalRows({ rows }: { rows: Array<[string, ReactNode]> }) {
  return (
    <dl className="tech">
      {rows.map(([key, value]) => (
        <div className="tech__row" key={key}>
          <dt className="tech__key">{key}</dt>
          <dd className="tech__val">{value}</dd>
        </div>
      ))}
    </dl>
  );
}
