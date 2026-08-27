import { Link } from "react-router-dom";
import type { ReactNode } from "react";
import "./MetricBand.css";

interface Metric {
  label: string;
  value: ReactNode;
  /** One line saying what the number means operationally. */
  note: string;
  to?: string;
  tone?: "attention" | "neutral" | "success";
}

/**
 * The primary operational figures.
 *
 * A divided band rather than a grid of cards: these numbers are read together,
 * and card chrome would separate what should be compared at a glance. Each
 * metric answers one operator question and links to where it is acted on.
 */
export function MetricBand({ metrics }: { metrics: Metric[] }) {
  return (
    <ul className="band" aria-label="Operational summary">
      {metrics.map((metric) => {
        const body = (
          <>
            <span className="band__label">{metric.label}</span>
            <span className={`band__value band__value--${metric.tone ?? "neutral"}`}>
              {metric.value}
            </span>
            <span className="band__note">{metric.note}</span>
          </>
        );
        return (
          <li className="band__item" key={metric.label}>
            {metric.to ? (
              <Link className="band__link" to={metric.to}>{body}</Link>
            ) : (
              <div className="band__link band__link--static">{body}</div>
            )}
          </li>
        );
      })}
    </ul>
  );
}
