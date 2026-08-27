import type { ReactNode } from "react";
import "./Section.css";

interface Props {
  title: string;
  /** One line explaining what this evidence is and how far it can be trusted. */
  note?: string;
  /** Trust tier. `advisory` recedes; `evidence` is bordered; `plain` is default. */
  tone?: "plain" | "evidence" | "advisory";
  aside?: ReactNode;
  children: ReactNode;
  id?: string;
}

/**
 * A titled block of case evidence.
 *
 * `tone` encodes the trust hierarchy: advisory content is visually recessed so
 * it can never outrank a financial fact, and evidence blocks are bordered to
 * mark them as things the system observed rather than concluded.
 */
export function Section({ title, note, tone = "plain", aside, children, id }: Props) {
  return (
    <section className={`section section--${tone}`} aria-labelledby={id ? `${id}-h` : undefined} id={id}>
      <header className="section__head">
        <div>
          <h2 className="section__title" id={id ? `${id}-h` : undefined}>{title}</h2>
          {note && <p className="section__note">{note}</p>}
        </div>
        {aside && <div className="section__aside">{aside}</div>}
      </header>
      <div className="section__body">{children}</div>
    </section>
  );
}
