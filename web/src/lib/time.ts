/**
 * Timestamp presentation.
 *
 * Operators reason in elapsed time ("40 minutes ago") but audit and disputes
 * need the absolute instant, so relative text always carries the exact
 * timestamp as its title.
 */

const MINUTE = 60_000;
const HOUR = 60 * MINUTE;
const DAY = 24 * HOUR;

export function parse(iso: string | null | undefined): Date | null {
  if (!iso) return null;
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? null : d;
}

/** "just now", "6m ago", "3h ago", "2d ago". Past only. */
export function relativeFromNow(iso: string | null | undefined, now = Date.now()): string {
  const d = parse(iso);
  if (!d) return "—";
  const delta = now - d.getTime();
  if (delta < 0) return "in the future";
  if (delta < MINUTE) return "just now";
  if (delta < HOUR) return `${Math.floor(delta / MINUTE)}m ago`;
  if (delta < DAY) return `${Math.floor(delta / HOUR)}h ago`;
  return `${Math.floor(delta / DAY)}d ago`;
}

/**
 * Signed distance to a deadline: `{ text, expired }`.
 * "in 41m" while the window is open, "closed 2h ago" once it has passed.
 */
export function deadlineDistance(
  iso: string | null | undefined,
  now = Date.now(),
): { text: string; expired: boolean } | null {
  const d = parse(iso);
  if (!d) return null;
  const delta = d.getTime() - now;
  const expired = delta <= 0;
  const magnitude = Math.abs(delta);

  let amount: string;
  if (magnitude < MINUTE) amount = "under a minute";
  else if (magnitude < HOUR) amount = `${Math.floor(magnitude / MINUTE)}m`;
  else if (magnitude < DAY) {
    const h = Math.floor(magnitude / HOUR);
    const m = Math.floor((magnitude % HOUR) / MINUTE);
    amount = m > 0 ? `${h}h ${m}m` : `${h}h`;
  } else amount = `${Math.floor(magnitude / DAY)}d`;

  return { text: expired ? `closed ${amount} ago` : `in ${amount}`, expired };
}

/** Compact instant for narrow columns: "27 Aug, 16:28". */
export function compact(iso: string | null | undefined): string {
  const d = parse(iso);
  if (!d) return "—";
  return d.toLocaleString(undefined, {
    day: "numeric", month: "short",
    hour: "2-digit", minute: "2-digit", hour12: false,
  });
}

/** Full, unambiguous instant for tooltips and forensic reading. */
export function absolute(iso: string | null | undefined): string {
  const d = parse(iso);
  if (!d) return "—";
  return d.toLocaleString(undefined, {
    year: "numeric", month: "short", day: "numeric",
    hour: "2-digit", minute: "2-digit", second: "2-digit",
  });
}
