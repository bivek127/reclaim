import { Fragment, useMemo } from "react";
import { useSearchParams } from "react-router-dom";
import { TimelineEvent } from "./TimelineEvent";
import { EmptyState } from "../States";
import { CATEGORIES, categoryOf, isRoutine } from "@/lib/events";
import type { EventCategory } from "@/lib/events";
import type { CaseHistory } from "@/lib/types";
import "./CaseTimeline.css";

interface Props {
  history: CaseHistory;
}

const CLOCK = new Intl.DateTimeFormat(undefined, {
  hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false,
});

/**
 * Clock time to millisecond precision. Events written in one transaction share
 * a timestamp, and without the fraction a reader cannot tell a genuine tie
 * from two events a few milliseconds apart.
 */
function clockOf(iso: string): string {
  const base = CLOCK.format(new Date(iso));
  const ms = /\.(\d{1,3})/.exec(iso)?.[1] ?? "";
  return ms ? `${base}.${ms.padEnd(3, "0")}` : base;
}

const DAY = new Intl.DateTimeFormat(undefined, {
  weekday: "short", day: "numeric", month: "short", year: "numeric",
});

/**
 * The forensic record for one case.
 *
 * Order is exactly the order the API returned — the reconstruction is sorted
 * by recorded time then sequence, and re-sorting here would misrepresent
 * causality. Filtering only hides rows; it never reorders them, and the
 * position number shown on an event is its position in the full record, not
 * in the filtered view.
 */
export function CaseTimeline({ history }: Props) {
  const [params, setParams] = useSearchParams();
  const active = params.getAll("cat") as EventCategory[];
  const showRoutine = params.get("routine") === "1";

  // Positions are assigned over the authoritative ordering, before filtering.
  const numbered = useMemo(
    () =>
      history.timeline.map((event, index) => {
        const previous = history.timeline[index - 1];
        return {
          event,
          position: index + 1,
          sharesInstant: previous?.occurred_at === event.occurred_at,
        };
      }),
    [history.timeline],
  );

  const present = useMemo(() => {
    const counts = new Map<EventCategory, number>();
    for (const { event } of numbered) {
      const c = categoryOf(event);
      counts.set(c, (counts.get(c) ?? 0) + 1);
    }
    return counts;
  }, [numbered]);

  const routineCount = numbered.filter((n) => isRoutine(n.event)).length;

  const visible = numbered.filter(({ event }) => {
    if (!showRoutine && isRoutine(event)) return false;
    if (active.length > 0 && !active.includes(categoryOf(event))) return false;
    return true;
  });

  function toggleCategory(category: EventCategory) {
    const next = new URLSearchParams(params);
    next.delete("cat");
    const wanted = active.includes(category)
      ? active.filter((c) => c !== category)
      : [...active, category];
    wanted.forEach((c) => next.append("cat", c));
    setParams(next, { replace: true });
  }

  function toggleRoutine() {
    const next = new URLSearchParams(params);
    if (showRoutine) next.delete("routine");
    else next.set("routine", "1");
    setParams(next, { replace: true });
  }

  function clear() {
    const next = new URLSearchParams(params);
    next.delete("cat");
    next.delete("routine");
    setParams(next, { replace: true });
  }

  const filtering = active.length > 0 || showRoutine;

  return (
    <div className="tl">
      <div className="tl__controls">
        <ul className="tl__cats" aria-label="Filter by event class">
          {[...present.entries()]
            .filter(([category]) => category !== "concurrency" || showRoutine)
            .map(([category, count]) => {
              const on = active.includes(category);
              return (
                <li key={category}>
                  <button
                    type="button"
                    className={`chip chip--sm${on ? " chip--on" : ""}`}
                    aria-pressed={on}
                    onClick={() => toggleCategory(category)}
                    title={CATEGORIES[category].blurb}
                  >
                    {CATEGORIES[category].label}
                    <span className="tl__count">{count}</span>
                  </button>
                </li>
              );
            })}
        </ul>

        <div className="tl__right">
          {routineCount > 0 && (
            <button
              type="button"
              className={`chip chip--sm${showRoutine ? " chip--on" : ""}`}
              aria-pressed={showRoutine}
              onClick={toggleRoutine}
              title="Lease claims and releases. Real evidence, but routine bookkeeping."
            >
              Routine concurrency
              <span className="tl__count">{routineCount}</span>
            </button>
          )}
          {filtering && (
            <button type="button" className="btn btn--ghost btn--sm" onClick={clear}>
              Reset
            </button>
          )}
        </div>
      </div>

      <p className="tl__summary" role="status">
        Showing <strong>{visible.length}</strong> of <strong>{history.timeline.length}</strong>{" "}
        recorded events
        {!showRoutine && routineCount > 0 && (
          <span className="tl__hidden"> · {routineCount} routine lease events hidden</span>
        )}
      </p>

      {visible.length === 0 ? (
        <EmptyState
          title="No events match this filter"
          description="Every recorded event is still in the case history; only the view is filtered."
          action={
            <button type="button" className="btn btn--secondary" onClick={clear}>
              Show everything
            </button>
          }
        />
      ) : (
        // A single list keeps one unbroken rail down the whole history; day
        // markers are items within it rather than nested lists.
        <ol className="tl__list" aria-label="Recorded events">
          {visible.map(({ event, position, sharesInstant }, index) => {
            const previousVisible = visible[index - 1]?.event;
            const day = DAY.format(new Date(event.occurred_at));
            const previousDay = previousVisible
              ? DAY.format(new Date(previousVisible.occurred_at))
              : null;
            return (
              <Fragment key={event.id}>
                {day !== previousDay && (
                  <li className="tl__day-item">
                    <p className="tl__day">{day}</p>
                  </li>
                )}
                <TimelineEvent
                  event={event}
                  position={position}
                  sharesInstant={sharesInstant}
                  clockTime={clockOf(event.occurred_at)}
                />
              </Fragment>
            );
          })}
        </ol>
      )}
    </div>
  );
}
