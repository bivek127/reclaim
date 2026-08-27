/**
 * Wire shapes returned by the Reclaim API.
 *
 * These mirror what the Python read model and audit package already produce.
 * All money is integer minor units; all timestamps are ISO-8601 strings.
 */

/** Liveness only. The API asserts nothing here about the provider or workers. */
export interface Health {
  status: string;
  environment: string;
}

export interface Meta {
  environment: string;
  case_states: string[];
  attention_states: string[];
  in_flight_states: string[];
  reviewable_actions: string[];
}

export interface CaseRow {
  case_id: number;
  state: string;
  amount_minor: number;
  currency: string;
  customer_ref: string;
  anchor_kind: string;
  anchor_key: string;
  attempt_count: number;
  max_attempts: number;
  recovered_amount_minor: number;
  created_at: string;
  updated_at: string;
  has_pending_review: boolean;
  action_deadline_at: string | null;
}

export interface CasePage {
  rows: CaseRow[];
  total: number;
  limit: number;
  offset: number;
}

export interface ActivityEvent {
  id: number;
  occurred_at: string;
  event_type: string;
  case_id: number | null;
  reason_code: string | null;
  prev_state: string | null;
  new_state: string | null;
  worker_id: string | null;
  reviewer_ref: string | null;
}

export interface Overview {
  state_counts: Record<string, number>;
  attention_total: number;
  in_flight_total: number;
  recovered_count: number;
  recovered_amount_minor: number;
  pending_reviews: number;
  oldest_pending_review_at: string | null;
  breaker_state: string;
  breaker_consecutive_failures: number;
  recent_activity: ActivityEvent[];
}

export interface Obligation {
  id: number;
  anchor_kind: string;
  anchor_key: string;
  anchor_canonical: string;
  amount_minor: number;
  currency: string;
  customer_ref: string;
  source_event_id: string;
  first_seen_at: string;
  last_seen_at: string;
}

export interface Diagnosis {
  id: number;
  source: string;
  model: string | null;
  model_version: string | null;
  prompt_version: string;
  cause: string;
  recommended_action: string | null;
  reasoning: string | null;
  confidence: number | null;
  llm_retry_count: number;
  created_at: string;
}

export interface PolicyDecision {
  id: number;
  diagnosis_id: number | null;
  policy_version: string;
  lookup_miss: boolean;
  conflicting_history: boolean;
  ambiguity_signal: boolean;
  verdict: string;
  selected_action: string | null;
  reason_code: string;
  created_at: string;
}

export interface RecoveryAction {
  id: number;
  action_type: string;
  status: string;
  sequence_no: number;
  policy_decision_id: number;
  superseded_by: number | null;
  provider_expires_at: string | null;
  action_deadline_at: string | null;
  created_at: string;
  resolved_at: string | null;
}

export interface ExecutionAttempt {
  id: number;
  action_id: number;
  attempt_no: number;
  idempotency_key: string;
  provider_reference: string | null;
  state: string;
  amount_minor: number;
  currency: string;
  created_at: string;
  settled_at: string | null;
}

export interface ProviderRequest {
  id: number;
  attempt_id: number;
  operation: string;
  request_no: number;
  idempotency_key: string;
  outcome: string;
  http_status: number | null;
  provider_correlation_id: string | null;
  sent_at: string;
  completed_at: string | null;
  response_body: unknown;
}

export interface Verification {
  id: number;
  attempt_id: number;
  webhook_event_id: number | null;
  webhook_status: string | null;
  query_status: string | null;
  query_correlation_id: string | null;
  agrees: boolean;
  verified_amount_minor: number;
  created_at: string;
}

export interface HumanReview {
  id: number;
  status: string;
  reviewer_ref: string | null;
  selected_action: string | null;
  review_expires_at: string;
  created_at: string;
  decided_at: string | null;
}

export interface CaseDetail {
  case: CaseRow;
  obligation: Obligation;
  diagnoses: Diagnosis[];
  policy_decisions: PolicyDecision[];
  actions: RecoveryAction[];
  attempts: ExecutionAttempt[];
  provider_requests: ProviderRequest[];
  verifications: Verification[];
  reviews: HumanReview[];
}

export interface AuditEvent {
  id: number;
  occurred_at: string;
  event_type: string;
  obligation_id: number | null;
  case_id: number | null;
  action_id: number | null;
  attempt_id: number | null;
  provider_request_id: number | null;
  worker_id: string | null;
  fencing_token: number | null;
  prev_state: string | null;
  new_state: string | null;
  reason_code: string | null;
  model: string | null;
  model_version: string | null;
  policy_version: string | null;
  reviewer_ref: string | null;
  provider_correlation_id: string | null;
  detail: Record<string, unknown>;
}

export interface StateChange {
  at: string;
  prev_state: string | null;
  new_state: string | null;
  reason_code: string | null;
  worker_id: string | null;
  fencing_token: number | null;
}

export interface CaseHistory {
  case_id: number | null;
  obligation_id: number | null;
  created: boolean;
  deduplicated: boolean;
  timeline: AuditEvent[];
  state_changes: StateChange[];
  provider_correlation_ids: string[];
  provider_references: string[];
  workers: string[];
  fencing_tokens: number[];
  stale_writes: AuditEvent[];
  /** Facts the audit trail could not supply. Shown, never hidden. */
  unreconstructable: string[];
}

export interface ReviewQueueRow {
  review_id: number;
  case_id: number;
  status: string;
  reviewer_ref: string | null;
  selected_action: string | null;
  review_expires_at: string;
  created_at: string;
  decided_at: string | null;
  case_state: string;
  amount_minor: number;
  currency: string;
  customer_ref: string;
  anchor_key: string;
}

export interface ReviewQueue {
  rows: ReviewQueueRow[];
  total: number;
  limit: number;
  offset: number;
  status: string;
}

export interface SystemStatus {
  breaker: {
    state: string;
    consecutive_failures: number;
    opened_at: string | null;
    reset_after: string | null;
    trip_cause: unknown;
  } | null;
  leases_held: number;
  leases_expired: number;
  open_actions: number;
  unresolved_attempts: number;
  stale_writes_rejected: number;
}
