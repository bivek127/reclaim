import type { Health, Meta } from "@/lib/types";
import "./AttachmentPanel.css";

interface Props {
  health: Health | undefined;
  meta: Meta | undefined;
  /** True when the liveness probe itself failed or has not answered. */
  unreachable: boolean;
}

/**
 * What this console is talking to.
 *
 * `environment` is the label the API reports for the database it opened, so it
 * identifies the attachment rather than a deployment tier the API never states.
 * Nothing here describes hosts, versions, or uptime: the API reports none of
 * those, and a control surface that guesses them is worse than one that omits
 * them.
 */
export function AttachmentPanel({ health, meta, unreachable }: Props) {
  const environment = health?.environment ?? meta?.environment ?? null;

  return (
    <dl className="att">
      <div className="att__item">
        <dt>Recovery API</dt>
        <dd>
          <span className={`att__dot att__dot--${unreachable ? "bad" : "ok"}`} aria-hidden="true" />
          {unreachable ? "Unreachable" : "Responding"}
        </dd>
      </div>

      <div className="att__item">
        <dt>Liveness</dt>
        <dd>
          {unreachable || !health ? (
            <span className="att__unknown">Not reported</span>
          ) : (
            <span className="u-mono">{health.status}</span>
          )}
        </dd>
      </div>

      <div className="att__item">
        <dt>Database</dt>
        <dd>
          {environment ? (
            <span className="u-mono">{environment}</span>
          ) : (
            <span className="att__unknown">Not reported</span>
          )}
        </dd>
      </div>

      <div className="att__item">
        <dt>Case states in use</dt>
        <dd>
          {meta ? `${meta.case_states.length} states` : <span className="att__unknown">Not reported</span>}
        </dd>
      </div>
    </dl>
  );
}
