import { Fragment } from "react";
import { useQuery } from "@tanstack/react-query";
import { PageHeader } from "@/components/PageHeader";
import { Identifier } from "@/components/Identifier";
import { RelativeTime } from "@/components/RelativeTime";
import { Disclosure, TechnicalRows } from "@/components/Disclosure";
import { EmptyState, ErrorState, SkeletonRows } from "@/components/States";
import { api } from "@/lib/api";

/**
 * Webhooks ingest could not anchor to any obligation (§1.3).
 *
 * No case exists for a row here -- that is the point of the state -- so there
 * is nothing to click through to. The payload is shown in full because it is
 * the only evidence an operator has for judging what the anchor should have
 * been; this page never proposes or performs that correction itself.
 */
export function UnmappablePage() {
  const unmappable = useQuery({
    queryKey: ["unmappable"],
    queryFn: () => api.unmappable(100, 0),
  });

  const rows = unmappable.data?.rows ?? [];

  return (
    <>
      <PageHeader
        title="Unmapped webhooks"
        description="Payloads a provider sent that could not be tied to any obligation. Never guessed at ingest; recorded here for a human to judge instead."
      />

      <div className="page-body">
        {unmappable.isError && (
          <div style={{ marginBottom: "var(--s-4)" }}>
            <ErrorState
              title="Could not load unmapped webhooks"
              error={unmappable.error}
              stale={unmappable.data !== undefined}
              onRetry={() => unmappable.refetch()}
            />
          </div>
        )}

        <section className="panel" aria-label="Unmapped webhooks">
          {unmappable.isPending ? (
            <SkeletonRows rows={5} columns={3} />
          ) : unmappable.isError && unmappable.data === undefined ? (
            // A failed read is not an empty queue: saying "nothing is waiting"
            // here would tell the reader the opposite of what is known.
            <EmptyState
              title="The unmapped-webhook queue could not be read"
              description="This is a failure to load, not an empty queue. Retry above."
            />
          ) : rows.length === 0 ? (
            <EmptyState
              title="Nothing is unmapped"
              description="Every webhook ingest has received so far resolved to an obligation, was ignored, or was recognisably malformed."
            />
          ) : (
            <div className="table-wrap">
              <table className="ctable">
                <caption className="u-visually-hidden">
                  Unmapped webhooks, oldest received first.
                </caption>
                <colgroup>
                  <col style={{ width: 220 }} />
                  <col />
                  <col style={{ width: 148 }} />
                </colgroup>
                <thead>
                  <tr>
                    <th scope="col">Event</th>
                    <th scope="col">Provider event id</th>
                    <th scope="col">Received</th>
                  </tr>
                </thead>
                <tbody>
                  {rows.map((row) => (
                    <Fragment key={row.webhook_event_id}>
                      <tr>
                        <td className="u-mono">{row.event_type}</td>
                        <td>
                          <Identifier value={row.provider_event_id} label="Provider event id" />
                        </td>
                        <td className="cell-time">
                          <RelativeTime value={row.received_at} />
                        </td>
                      </tr>
                      <tr>
                        <td colSpan={3} style={{ paddingTop: 0 }}>
                          <Disclosure label="Raw payload">
                            <TechnicalRows
                              rows={[
                                [
                                  "payload",
                                  <pre className="u-mono">
                                    {JSON.stringify(row.payload, null, 2)}
                                  </pre>,
                                ],
                              ]}
                            />
                          </Disclosure>
                        </td>
                      </tr>
                    </Fragment>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
      </div>
    </>
  );
}
