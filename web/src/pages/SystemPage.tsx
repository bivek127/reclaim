import { Link } from "react-router-dom";
import { useQuery } from "@tanstack/react-query";
import { PageHeader } from "@/components/PageHeader";
import { Section } from "@/components/Section";
import { EmptyState, ErrorState, SkeletonBlock } from "@/components/States";
import { BreakerPanel } from "@/components/system/BreakerPanel";
import { AttachmentPanel } from "@/components/system/AttachmentPanel";
import { SafetySignals } from "@/components/system/SafetySignals";
import { SystemEvents } from "@/components/system/SystemEvents";
import { StateVocabulary } from "@/components/system/StateVocabulary";
import { api } from "@/lib/api";
import "./SystemPage.css";

/**
 * Whether Reclaim itself is working, and what it is attached to.
 *
 * Deliberately not an infrastructure dashboard. The API reports no CPU,
 * memory, latency, throughput, uptime or error rate, so none is drawn — a
 * plausible-looking chart over data the backend does not keep would be the
 * most dangerous thing on an operations screen.
 *
 * Each panel is queried independently and fails independently: one dead
 * endpoint blanks its own section rather than the page, and no section
 * substitutes a zero for a value it could not read.
 */
export function SystemPage() {
  const health = useQuery({ queryKey: ["health"], queryFn: api.health, refetchInterval: 30_000, retry: false });
  const system = useQuery({ queryKey: ["system"], queryFn: api.system, refetchInterval: 30_000 });
  const meta = useQuery({ queryKey: ["meta"], queryFn: api.meta, staleTime: 5 * 60 * 1000 });
  const overview = useQuery({ queryKey: ["overview"], queryFn: api.overview, refetchInterval: 30_000 });

  // Liveness is the authority on reachability. Until it answers, nothing is
  // asserted either way; a pending probe is not evidence of health.
  const unreachable = health.isError;
  const statusUnknown = system.isError || system.data === undefined;

  return (
    <>
      <PageHeader
        title="System"
        description="What this console is attached to, and whether the recovery machinery is running."
      />

      <div className="page-body sysp">
        {system.isPending ? (
          <SkeletonBlock height={96} />
        ) : (
          <BreakerPanel breaker={system.data?.breaker ?? null} indeterminate={statusUnknown} />
        )}

        {system.isError && (
          <ErrorState
            title="System status is unavailable"
            error={system.error}
            onRetry={() => system.refetch()}
          />
        )}

        <div className="sysp__grid">
          <Section
            title="Concurrency and safety"
            note="Counters from the fencing, lease, and execution machinery. A rejected stale write is the protection working."
          >
            {system.isPending ? (
              <SkeletonBlock height={200} />
            ) : statusUnknown ? (
              <EmptyState
                title="Counters could not be read"
                description="No figures are shown. An unreadable counter is not a zero."
              />
            ) : (
              <SafetySignals system={system.data} />
            )}
          </Section>

          <div className="sysp__side">
            <Section title="Attachment" note="The service and database this console is reading.">
              {health.isPending && meta.isPending ? (
                <SkeletonBlock height={44} />
              ) : (
                <AttachmentPanel health={health.data} meta={meta.data} unreachable={unreachable} />
              )}
            </Section>

            <Section
              title="System events"
              note="Audit events recorded against no case. These cannot appear in a case timeline."
            >
              {overview.isPending ? (
                <SkeletonBlock height={140} />
              ) : overview.isError ? (
                <ErrorState
                  title="System events are unavailable"
                  error={overview.error}
                  onRetry={() => overview.refetch()}
                />
              ) : (
                <SystemEvents activity={overview.data.recent_activity} />
              )}
            </Section>
          </div>
        </div>

        <nav className="sysp__nav" aria-label="Operational surfaces">
          <span className="u-label">Where to look next</span>
          <Link to="/cases?needs_attention=true">Cases needing a human</Link>
          <Link to="/reviews">Pending reviews</Link>
          <Link to="/cases">All cases</Link>
        </nav>

        <Section
          title="Case state vocabulary"
          note="Reported by the API from the domain. The console does not keep its own list."
        >
          {meta.isPending ? (
            <SkeletonBlock height={220} />
          ) : meta.isError ? (
            <ErrorState
              title="State vocabulary is unavailable"
              error={meta.error}
              onRetry={() => meta.refetch()}
            />
          ) : (
            <StateVocabulary meta={meta.data} />
          )}
        </Section>
      </div>
    </>
  );
}
