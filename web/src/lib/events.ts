/**
 * Presentation vocabulary for audit events.
 *
 * Every title and summary below is composed from fields the API actually
 * returned for that event. Nothing is inferred from case state, and an event
 * type this module does not recognise is shown as itself rather than being
 * folded into a vague "activity" bucket.
 */

import type { AuditEvent } from "./types";

export type EventCategory =
  | "case" | "diagnosis" | "policy" | "execution" | "provider"
  | "verification" | "review" | "state" | "concurrency" | "system" | "other";

export interface CategoryMeta {
  label: string;
  /** One line on what this class of event represents. */
  blurb: string;
  /** Advisory categories are recessed; they never outrank a financial fact. */
  advisory?: boolean;
}

export const CATEGORIES: Record<EventCategory, CategoryMeta> = {
  case:         { label: "Case",         blurb: "Case lifecycle bookkeeping." },
  state:        { label: "State change", blurb: "A transition of the authoritative case state." },
  policy:       { label: "Policy",       blurb: "Deterministic decisions that authorise or refuse an action." },
  execution:    { label: "Execution",    blurb: "Requests this system sent to the provider." },
  provider:     { label: "Provider",     blurb: "What the provider reported back. A claim, not a verification." },
  verification: { label: "Verification", blurb: "Independent confirmation before revenue is recognised." },
  review:       { label: "Review",       blurb: "Decisions made by a human reviewer." },
  concurrency:  { label: "Concurrency",  blurb: "Leases and fencing. A rejected stale write is a safety event, not a failure." },
  system:       { label: "System",       blurb: "Circuit breaker and other system-wide events." },
  diagnosis:    { label: "Diagnosis",    blurb: "Advisory classification of the failure. It authorises nothing.", advisory: true },
  other:        { label: "Other",        blurb: "Events this console does not yet describe." },
};

const TYPE_CATEGORY: Record<string, EventCategory> = {
  case_created: "case",
  case_deduplicated: "case",
  state_transition: "state",
  policy_decision: "policy",
  diagnosis_produced: "diagnosis",
  provider_request_sent: "execution",
  provider_response_received: "provider",
  provider_response_observed: "provider",
  reconciliation_query_sent: "provider",
  reconciliation_result: "provider",
  verification_recorded: "verification",
  review_decision: "review",
  lease_claimed: "concurrency",
  lease_released: "concurrency",
  stale_write_rejected: "concurrency",
  breaker_opened: "system",
  breaker_reset: "system",
};

export function categoryOf(event: AuditEvent): EventCategory {
  return TYPE_CATEGORY[event.event_type] ?? "other";
}

/** Routine bookkeeping: real evidence, but not what an operator reads first. */
export function isRoutine(event: AuditEvent): boolean {
  return event.event_type === "lease_claimed" || event.event_type === "lease_released";
}

function str(detail: Record<string, unknown>, key: string): string | null {
  const v = detail[key];
  return v === null || v === undefined ? null : String(v);
}

/** Human title, composed only from what the event carries. */
export function titleOf(event: AuditEvent): string {
  const d = event.detail ?? {};
  switch (event.event_type) {
    case "case_created": return "Case opened";
    case "case_deduplicated": return "Duplicate delivery ignored";
    case "state_transition": return "State changed";
    case "diagnosis_produced": return "Failure classified";
    case "policy_decision": return "Policy decided";
    case "provider_request_sent": return `Request sent — ${str(d, "operation") ?? "provider call"}`;
    case "provider_response_received": return `Provider answered — ${str(d, "provider_outcome") ?? "outcome recorded"}`;
    case "provider_response_observed": return "Provider answer observed but not applied";
    case "reconciliation_query_sent": return "Reconciliation query sent";
    case "reconciliation_result": return `Reconciliation result — ${str(d, "fetch_outcome") ?? "recorded"}`;
    case "verification_recorded": return d["agrees"] === true ? "Independently verified" : "Verification disagreed";
    case "review_decision": return `Reviewer ${String(str(d, "status") ?? "decided").toLowerCase()}`;
    case "lease_claimed": return "Lease claimed";
    case "lease_released": return "Lease released";
    case "stale_write_rejected": return "Stale write rejected";
    case "breaker_opened": return "Circuit breaker opened";
    case "breaker_reset": return "Circuit breaker reset";
    default: return event.event_type;
  }
}

/**
 * One-line explanation. Returns null when the event carries nothing beyond its
 * title — an empty line is better than invented narration.
 */
export function summaryOf(event: AuditEvent): string | null {
  const d = event.detail ?? {};
  switch (event.event_type) {
    case "case_created": {
      const anchor = str(d, "anchor_canonical");
      return anchor ? `Opened against ${anchor}.` : null;
    }
    case "diagnosis_produced": {
      const cause = str(d, "cause");
      const source = str(d, "source");
      if (!cause) return null;
      const how = source === "LLM" ? "by the model" : "by the deterministic fallback";
      return `Classified as ${cause} ${how}. Advisory only.`;
    }
    case "policy_decision": {
      const verdict = str(d, "verdict");
      const action = str(d, "selected_action");
      if (!verdict) return null;
      if (verdict === "ALLOW" && action) return `Permitted ${action}.`;
      if (verdict === "ESCALATE") return "Refused to act automatically; handed to a human.";
      if (verdict === "NO_ACTION") return "No action permitted for this cause.";
      return `Verdict ${verdict}.`;
    }
    case "provider_request_sent": {
      const ref = str(d, "provider_reference");
      return ref ? `Sent under reference ${ref}.` : null;
    }
    case "provider_response_received": {
      const outcome = str(d, "provider_outcome");
      const target = str(d, "target_state");
      if (!outcome) return null;
      return target
        ? `Provider reported ${outcome}; the case moved toward ${target}.`
        : `Provider reported ${outcome}.`;
    }
    case "provider_response_observed":
      return "The provider answered, but a newer worker held the case, so the answer was recorded without being applied.";
    case "reconciliation_result": {
      const outcome = str(d, "fetch_outcome");
      const sent = d["post_provably_sent"];
      if (!outcome) return null;
      if (outcome === "NOT_FOUND" && sent === false) {
        return "Nothing found, but the original request may never have been sent — treated as inconclusive.";
      }
      if (outcome === "NOT_FOUND") return "Nothing found, and the request provably went out — confirmed failure.";
      if (outcome === "NO_EVIDENCE") return "The query returned no usable evidence. Not treated as absence.";
      return `Provider returned ${outcome}.`;
    }
    case "verification_recorded": {
      const webhook = str(d, "webhook_status");
      const query = str(d, "query_status");
      if (d["agrees"] === true) {
        return `Webhook claimed ${webhook ?? "success"} and an independent query returned ${query ?? "paid"}; both agree.`;
      }
      return `Webhook claimed ${webhook ?? "unknown"} but the independent query returned ${query ?? "no match"}. No revenue recognised.`;
    }
    case "review_decision": {
      const action = str(d, "selected_action");
      const status = str(d, "status");
      if (status === "APPROVED") {
        return action
          ? `Proposed ${action} for the executor to dispatch. The reviewer does not dispatch it.`
          : "Approved.";
      }
      if (status === "REJECTED") return "Rejected; the case was closed as not recovered.";
      return null;
    }
    case "lease_claimed":
      return event.worker_id ? `${event.worker_id} took the case under fencing token ${event.fencing_token}.` : null;
    case "stale_write_rejected":
      return "A worker holding an older fencing token tried to write and was refused. The case was protected.";
    default:
      return null;
  }
}

/** Tone for the status marker. Neutral by default: not every event is an alarm. */
export type EventTone = "neutral" | "positive" | "attention" | "advisory";

export function toneOf(event: AuditEvent): EventTone {
  const d = event.detail ?? {};
  switch (event.event_type) {
    case "verification_recorded":
      return d["agrees"] === true ? "positive" : "attention";
    case "policy_decision":
      return str(d, "verdict") === "ESCALATE" ? "attention" : "neutral";
    case "provider_response_received": {
      const outcome = str(d, "provider_outcome");
      if (outcome === "ACCEPTED" || outcome === "DUPLICATE_REFERENCE") return "positive";
      return "attention";
    }
    case "stale_write_rejected":
    case "provider_response_observed":
    case "breaker_opened":
      return "attention";
    case "diagnosis_produced":
      return "advisory";
    default:
      return "neutral";
  }
}
