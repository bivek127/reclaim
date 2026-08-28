import { Identifier } from "../Identifier";
import { Money } from "../Money";
import { Disclosure, TechnicalRows } from "../Disclosure";
import { absolute, relativeFromNow } from "@/lib/time";
import { isTerminal } from "@/lib/states";
import type { CaseDetail, ExecutionAttempt, ProviderRequest, RecoveryAction } from "@/lib/types";
import "./RecoveryProgress.css";

/**
 * The recovery spine: Action → Attempt → Provider request.
 *
 * The nesting is the domain model, not a layout choice. An action is the
 * mechanism the system opened; an attempt is one dispatch of it under a
 * persisted idempotency key; a provider request is a single call made under
 * that key. Flattening these would hide why a retry is not a second charge.
 */

const ACTION_STATUS_NOTE: Record<string, string> = {
  PROPOSED: "Approved by a reviewer. Waiting for the executor to dispatch it.",
  LIVE: "Open at the provider. The customer can still pay.",
  // Same stored status, but the forward-looking half of that sentence is only
  // true while the case can still receive a payment.
  LIVE_ON_CLOSED_CASE:
    "Still open at the provider, although this case is already closed.",
  UNRESOLVED: "The provider's outcome is unknown. Nothing may replace it yet.",
  TERMINAL_FAILED: "Closed. The provider did not create a usable mechanism.",
  TERMINAL_SUCCESS: "Closed successfully.",
  SUPERSEDED: "Replaced by a later action.",
};

const ATTEMPT_STATE_NOTE: Record<string, string> = {
  PREPARED: "Committed locally; the call may never have left the process.",
  IN_FLIGHT: "Sent, awaiting the provider's answer.",
  ACCEPTED: "The provider accepted this attempt.",
  REJECTED: "The provider refused it, or nothing was ever sent.",
  UNKNOWN: "No usable answer came back. Treated as unresolved, never as failure.",
};

function ProviderCall({ request }: { request: ProviderRequest }) {
  const failed = !["ACCEPTED", "DUPLICATE_REFERENCE"].includes(request.outcome);
  return (
    <li className="call">
      <div className="call__row">
        <span className={`call__outcome${failed ? " is-failed" : " is-ok"}`}>
          {request.outcome}
        </span>
        <span className="call__op u-mono">{request.operation}</span>
        {request.http_status !== null && (
          <span className="call__http">HTTP {request.http_status}</span>
        )}
        <span className="call__time" title={absolute(request.sent_at)}>
          {relativeFromNow(request.sent_at)}
        </span>
      </div>
      {request.provider_correlation_id && (
        <div className="call__corr">
          <span className="u-label">Provider id</span>
          <Identifier value={request.provider_correlation_id} label="Provider correlation id" />
        </div>
      )}
      <Disclosure label="Request details">
        <TechnicalRows
          rows={[
            ["Request id", String(request.id)],
            ["Request number", String(request.request_no)],
            ["Idempotency key", <Identifier key="k" value={request.idempotency_key} />],
            ["Correlation id", <Identifier key="c" value={request.provider_correlation_id} />],
            ["Sent", absolute(request.sent_at)],
            ["Completed", absolute(request.completed_at)],
            [
              "Response",
              <pre className="tech__json" key="b">
                {JSON.stringify(request.response_body ?? null, null, 2)}
              </pre>,
            ],
          ]}
        />
      </Disclosure>
    </li>
  );
}

function AttemptBlock({
  attempt, requests,
}: { attempt: ExecutionAttempt; requests: ProviderRequest[] }) {
  return (
    <li className="attempt">
      <div className="attempt__head">
        <span className="attempt__no">Attempt {attempt.attempt_no}</span>
        <span className={`attempt__state state-${attempt.state.toLowerCase()}`}>
          {attempt.state}
        </span>
        <Money minorUnits={attempt.amount_minor} currency={attempt.currency} />
        <span className="attempt__time" title={absolute(attempt.created_at)}>
          {relativeFromNow(attempt.created_at)}
        </span>
      </div>
      <p className="attempt__note">
        {ATTEMPT_STATE_NOTE[attempt.state] ?? "Attempt state not recognised."}
      </p>

      <div className="attempt__key">
        <span className="u-label">Provider reference</span>
        <Identifier value={attempt.provider_reference} truncate label="Provider reference" />
      </div>

      {requests.length > 0 ? (
        <ul className="calls">{requests.map((r) => <ProviderCall key={r.id} request={r} />)}</ul>
      ) : (
        <p className="attempt__empty">No provider call was recorded for this attempt.</p>
      )}

      <Disclosure label="Attempt details">
        <TechnicalRows
          rows={[
            ["Attempt id", String(attempt.id)],
            ["Action id", String(attempt.action_id)],
            ["Idempotency key", <Identifier key="k" value={attempt.idempotency_key} />],
            ["Created", absolute(attempt.created_at)],
            ["Settled", absolute(attempt.settled_at)],
          ]}
        />
      </Disclosure>
    </li>
  );
}

function noteFor(status: string, caseClosed: boolean): string {
  if (status === "LIVE" && caseClosed) return ACTION_STATUS_NOTE["LIVE_ON_CLOSED_CASE"]!;
  return ACTION_STATUS_NOTE[status] ?? "Action status not recognised.";
}

function ActionBlock({
  action, attempts, requests, caseClosed,
}: {
  action: RecoveryAction;
  attempts: ExecutionAttempt[];
  requests: ProviderRequest[];
  caseClosed: boolean;
}) {
  const mine = attempts.filter((a) => a.action_id === action.id);
  const proposed = action.status === "PROPOSED";
  return (
    <li className={`action${proposed ? " is-proposed" : ""}`}>
      <div className="action__head">
        <span className="action__seq">Action {action.sequence_no}</span>
        <span className="action__type u-mono">{action.action_type}</span>
        <span className={`action__status status-${action.status.toLowerCase()}`}>
          {action.status}
        </span>
        <span className="action__time" title={absolute(action.created_at)}>
          {relativeFromNow(action.created_at)}
        </span>
      </div>
      <p className="action__note">
        {noteFor(action.status, caseClosed)}
      </p>

      {mine.length > 0 ? (
        <ul className="attempts">
          {mine.map((a) => (
            <AttemptBlock
              key={a.id}
              attempt={a}
              requests={requests.filter((r) => r.attempt_id === a.id)}
            />
          ))}
        </ul>
      ) : (
        <p className="action__empty">
          {proposed
            ? "Not dispatched yet — the executor performs the dispatch, not the reviewer."
            : "No execution attempt was made against this action."}
        </p>
      )}

      <Disclosure label="Action details">
        <TechnicalRows
          rows={[
            ["Action id", String(action.id)],
            ["Policy decision id", String(action.policy_decision_id)],
            ["Provider expiry", absolute(action.provider_expires_at)],
            ["Internal deadline", absolute(action.action_deadline_at)],
            ["Created", absolute(action.created_at)],
            ["Resolved", absolute(action.resolved_at)],
            ["Superseded by", action.superseded_by ? String(action.superseded_by) : "—"],
          ]}
        />
      </Disclosure>
    </li>
  );
}

export function RecoveryProgress({ detail }: { detail: CaseDetail }) {
  if (detail.actions.length === 0) {
    return (
      <p className="progress__empty">
        No recovery action was ever opened for this case, so nothing was sent to the
        provider and no money could have moved.
      </p>
    );
  }
  return (
    <ol className="actions">
      {detail.actions.map((a) => (
        <ActionBlock
          key={a.id}
          action={a}
          attempts={detail.attempts}
          requests={detail.provider_requests}
          caseClosed={isTerminal(detail.case.state)}
        />
      ))}
    </ol>
  );
}
