import { relativeFromNow, absolute } from "@/lib/time";

interface Props {
  value: string | null | undefined;
  className?: string;
}

/** Elapsed time for scanning, with the exact instant available on hover. */
export function RelativeTime({ value, className }: Props) {
  if (!value) return <span className={className}>—</span>;
  return (
    <time dateTime={value} title={absolute(value)} className={className}>
      {relativeFromNow(value)}
    </time>
  );
}
