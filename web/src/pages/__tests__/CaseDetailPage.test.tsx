import { describe, expect, it, vi, afterEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { CaseDetailPage } from "../CaseDetailPage";
import { api, ApiError } from "@/lib/api";
import type { CaseDetail, CaseHistory } from "@/lib/types";

function detail(over: Partial<CaseDetail> = {}): CaseDetail {
  return {
    case: {
      case_id: 1, state: "VERIFIED_RECOVERED", amount_minor: 425000, currency: "INR",
      customer_ref: "cust_ravi_8812", anchor_kind: "ORDER", anchor_key: "ord_4101",
      attempt_count: 1, max_attempts: 2, recovered_amount_minor: 425000,
      created_at: "2026-08-27T10:00:00+05:30", updated_at: "2026-08-27T10:05:00+05:30",
      has_pending_review: false, action_deadline_at: "2026-08-27T11:00:00+05:30",
    },
    obligation: {
      id: 1, anchor_kind: "ORDER", anchor_key: "ord_4101",
      anchor_canonical: "order:ord_4101", amount_minor: 425000, currency: "INR",
      customer_ref: "cust_ravi_8812", source_event_id: "evt_ord_4101",
      first_seen_at: "2026-08-27T10:00:00+05:30", last_seen_at: "2026-08-27T10:00:00+05:30",
    },
    diagnoses: [{
      id: 1, source: "DETERMINISTIC_FALLBACK", model: null, model_version: null,
      prompt_version: "v1", cause: "UNKNOWN", recommended_action: "CREATE_PAYMENT_LINK",
      reasoning: "deterministic fallback", confidence: null, llm_retry_count: 0,
      created_at: "2026-08-27T10:01:00+05:30",
    }],
    policy_decisions: [{
      id: 1, diagnosis_id: 1, policy_version: "1.0", lookup_miss: false,
      conflicting_history: false, ambiguity_signal: false, verdict: "ALLOW",
      selected_action: "CREATE_PAYMENT_LINK", reason_code: "policy_allow_create_link",
      created_at: "2026-08-27T10:02:00+05:30",
    }],
    actions: [{
      id: 1, action_type: "CREATE_PAYMENT_LINK", status: "LIVE", sequence_no: 1,
      policy_decision_id: 1, superseded_by: null,
      provider_expires_at: "2026-08-27T11:00:00+05:30",
      action_deadline_at: "2026-08-27T11:10:00+05:30",
      created_at: "2026-08-27T10:03:00+05:30", resolved_at: null,
    }],
    attempts: [{
      id: 1, action_id: 1, attempt_no: 1, idempotency_key: "rcv_ABC123",
      provider_reference: "rcv_ABC123", state: "ACCEPTED", amount_minor: 425000,
      currency: "INR", created_at: "2026-08-27T10:03:00+05:30",
      settled_at: "2026-08-27T10:04:00+05:30",
    }],
    provider_requests: [{
      id: 1, attempt_id: 1, operation: "create_payment_link", request_no: 1,
      idempotency_key: "rcv_ABC123", outcome: "ACCEPTED", http_status: 200,
      provider_correlation_id: "plink_XYZ", sent_at: "2026-08-27T10:03:00+05:30",
      completed_at: "2026-08-27T10:04:00+05:30", response_body: { id: "plink_XYZ" },
    }],
    verifications: [{
      id: 1, attempt_id: 1, webhook_event_id: 2, webhook_status: "SUCCESS",
      query_status: "PAID", query_correlation_id: "plink_XYZ", agrees: true,
      verified_amount_minor: 425000, created_at: "2026-08-27T10:05:00+05:30",
    }],
    reviews: [],
    ...over,
  };
}

function history(over: Partial<CaseHistory> = {}): CaseHistory {
  return {
    case_id: 1, obligation_id: 1, created: true, deduplicated: false,
    timeline: [], state_changes: [
      { at: "2026-08-27T10:01:00+05:30", prev_state: "NEW", new_state: "ENRICHING",
        reason_code: "enrichment_started", worker_id: "enrichment", fencing_token: 1 },
      { at: "2026-08-27T10:05:00+05:30", prev_state: "AWAITING_CUSTOMER",
        new_state: "VERIFIED_RECOVERED", reason_code: "verification_agreed",
        worker_id: "verifier", fencing_token: 6 },
    ],
    provider_correlation_ids: ["plink_XYZ"], provider_references: ["rcv_ABC123"],
    workers: ["verifier"], fencing_tokens: [1, 6], stale_writes: [],
    unreconstructable: [], ...over,
  };
}

function renderCase(path = "/cases/1") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/cases/:caseId" element={<CaseDetailPage />} />
          <Route path="/cases/:caseId/timeline" element={<CaseDetailPage />} />
          <Route path="/cases" element={<p>queue</p>} />
          <Route path="/reviews/:caseId" element={<p>review workspace</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.restoreAllMocks());

describe("case investigation workspace", () => {
  it("shows a structural skeleton while loading, not an error or empty page", async () => {
    vi.spyOn(api, "case").mockReturnValue(new Promise(() => {}));
    vi.spyOn(api, "timeline").mockReturnValue(new Promise(() => {}));
    const { container } = renderCase();
    await waitFor(() =>
      expect(container.querySelector('[aria-busy="true"]')).toBeInTheDocument(),
    );
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("renders the case identity, state, and obligation amount", async () => {
    vi.spyOn(api, "case").mockResolvedValue(detail());
    vi.spyOn(api, "timeline").mockResolvedValue(history());
    renderCase();
    // "Case #1" appears in the loading branch too, so wait for success-only content.
    expect(await screen.findByText("Verified Recovered")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Case #1" })).toBeInTheDocument();
    expect(screen.getAllByText("₹4,250.00").length).toBeGreaterThan(0);
    expect(screen.getAllByText("cust_ravi_8812").length).toBeGreaterThan(0);
  });

  it("distinguishes recovered revenue from the amount owed", async () => {
    vi.spyOn(api, "case").mockResolvedValue(detail());
    vi.spyOn(api, "timeline").mockResolvedValue(history());
    renderCase();
    await screen.findByText("Verified Recovered");
    // The revenue figure is labelled as verified, distinct from the amount owed.
    expect(screen.getByText(/verified recovered/)).toBeInTheDocument();
    expect(screen.getByText("Amount owed")).toBeInTheDocument();
  });

  it("states plainly when no revenue has been recognised", async () => {
    vi.spyOn(api, "case").mockResolvedValue(
      detail({ case: { ...detail().case, state: "AMBIGUOUS", recovered_amount_minor: 0 } }),
    );
    vi.spyOn(api, "timeline").mockResolvedValue(history());
    renderCase();
    expect(await screen.findByText("No revenue recognised")).toBeInTheDocument();
  });

  it("builds the lifecycle only from audit state changes", async () => {
    vi.spyOn(api, "case").mockResolvedValue(detail());
    vi.spyOn(api, "timeline").mockResolvedValue(history());
    renderCase();
    expect(await screen.findByText("enrichment_started")).toBeInTheDocument();
    expect(screen.getByText("verification_agreed")).toBeInTheDocument();
  });

  it("says so when the audit trail records no transitions", async () => {
    vi.spyOn(api, "case").mockResolvedValue(detail());
    vi.spyOn(api, "timeline").mockResolvedValue(history({ state_changes: [] }));
    renderCase();
    expect(
      await screen.findByText(/records no state transitions/i),
    ).toBeInTheDocument();
  });

  it("surfaces an audit evidence gap instead of hiding it", async () => {
    vi.spyOn(api, "case").mockResolvedValue(detail());
    vi.spyOn(api, "timeline").mockResolvedValue(
      history({ unreconstructable: ["provider_correlation_id"] }),
    );
    renderCase();
    expect(await screen.findByText("Evidence gap")).toBeInTheDocument();
    expect(screen.getByText("provider_correlation_id")).toBeInTheDocument();
  });

  it("preserves the action, attempt, and provider hierarchy", async () => {
    vi.spyOn(api, "case").mockResolvedValue(detail());
    vi.spyOn(api, "timeline").mockResolvedValue(history());
    renderCase();
    expect(await screen.findByText("Action 1")).toBeInTheDocument();
    expect(screen.getByText("Attempt 1")).toBeInTheDocument();
    expect(screen.getByText("create_payment_link")).toBeInTheDocument();
    expect(screen.getByText("HTTP 200")).toBeInTheDocument();
  });

  it("marks an approved-but-undispatched action as not yet executed", async () => {
    const d = detail();
    vi.spyOn(api, "case").mockResolvedValue({
      ...d,
      actions: [{ ...d.actions[0]!, id: 9, status: "PROPOSED", sequence_no: 2 }],
      attempts: [], provider_requests: [],
    });
    vi.spyOn(api, "timeline").mockResolvedValue(history());
    renderCase();
    expect(await screen.findByText(/the executor performs the dispatch/i)).toBeInTheDocument();
  });

  it("presents verification as two independent sources that agree", async () => {
    vi.spyOn(api, "case").mockResolvedValue(detail());
    vi.spyOn(api, "timeline").mockResolvedValue(history());
    renderCase();
    expect(await screen.findByText("Independently verified")).toBeInTheDocument();
    expect(screen.getByText(/Source 1/i)).toBeInTheDocument();
    expect(screen.getByText(/Source 2/i)).toBeInTheDocument();
    expect(screen.getByText("SUCCESS")).toBeInTheDocument();
    expect(screen.getByText("PAID")).toBeInTheDocument();
  });

  it("never presents an unverified case as recovered", async () => {
    vi.spyOn(api, "case").mockResolvedValue(detail({ verifications: [] }));
    vi.spyOn(api, "timeline").mockResolvedValue(history());
    renderCase();
    expect(await screen.findByText("Not verified")).toBeInTheDocument();
    expect(screen.queryByText("Independently verified")).not.toBeInTheDocument();
  });

  it("separates advisory diagnosis from the authoritative policy decision", async () => {
    vi.spyOn(api, "case").mockResolvedValue(detail());
    vi.spyOn(api, "timeline").mockResolvedValue(history());
    renderCase();
    // Diagnosis is labelled advisory and disclaims authority.
    expect(await screen.findByText(/does not authorise anything/i, {}, { timeout: 3000 })).toBeInTheDocument();
    expect(screen.getByText(/the policy table decides what is permitted/i)).toBeInTheDocument();
    // Policy is labelled deterministic and authoritative.
    expect(screen.getByText(/This is what authorised/i)).toBeInTheDocument();
    // Rendered uppercase by CSS; the accessible text is what is asserted here.
    expect(screen.getByText("advisory")).toBeInTheDocument();
    expect(screen.getByText("authoritative")).toBeInTheDocument();
    expect(screen.getByText("deterministic")).toBeInTheDocument();
  });

  it("shows the deterministic inputs behind a policy verdict", async () => {
    vi.spyOn(api, "case").mockResolvedValue(detail());
    vi.spyOn(api, "timeline").mockResolvedValue(history());
    renderCase();
    expect(await screen.findByText("policy_allow_create_link")).toBeInTheDocument();
    expect(screen.getByText("Ambiguity signal")).toBeInTheDocument();
    expect(screen.getAllByText("Not raised").length).toBeGreaterThan(0);
  });

  it("explains why a case reached a human and routes to the review workspace", async () => {
    const d = detail();
    vi.spyOn(api, "case").mockResolvedValue({
      ...d,
      case: { ...d.case, state: "ESCALATED", has_pending_review: true, recovered_amount_minor: 0 },
      policy_decisions: [
        ...d.policy_decisions,
        { ...d.policy_decisions[0]!, id: 2, verdict: "ESCALATE", selected_action: null,
          reason_code: "policy_escalate_budget" },
      ],
      reviews: [{
        id: 1, status: "PENDING", reviewer_ref: null, selected_action: null,
        review_expires_at: "2026-08-28T10:00:00+05:30",
        created_at: "2026-08-27T10:06:00+05:30", decided_at: null,
      }],
    });
    vi.spyOn(api, "timeline").mockResolvedValue(history());
    renderCase();
    expect(await screen.findByText(/every permitted attempt was used/i)).toBeInTheDocument();
    const cta = screen.getByRole("link", { name: /open review workspace/i });
    expect(cta).toHaveAttribute("href", "/reviews/1");
    // The reviewer proposes; the executor dispatches.
    expect(screen.getByText(/does not move money directly/i)).toBeInTheDocument();
  });

  it("omits the review section entirely when no human was involved", async () => {
    vi.spyOn(api, "case").mockResolvedValue(detail());
    vi.spyOn(api, "timeline").mockResolvedValue(history());
    renderCase();
    await screen.findByRole("heading", { name: "Case #1" });
    expect(screen.queryByText("Human review")).not.toBeInTheDocument();
  });

  it("navigates to Events & logs without losing case context", async () => {
    vi.spyOn(api, "case").mockResolvedValue(detail());
    vi.spyOn(api, "timeline").mockResolvedValue(history());
    const user = userEvent.setup();
    renderCase();
    const tab = (await screen.findAllByRole("link", { name: /events & logs/i }))[0]!;
    await user.click(tab);
    expect(await screen.findByText(/audit events/i)).toBeInTheDocument();
    // The case header stays: context is preserved across the tab change.
    expect(screen.getByRole("heading", { name: "Case #1" })).toBeInTheDocument();
  });

  it("reveals technical identifiers only on request", async () => {
    vi.spyOn(api, "case").mockResolvedValue(detail());
    vi.spyOn(api, "timeline").mockResolvedValue(history());
    const user = userEvent.setup();
    renderCase();
    const toggle = await screen.findByRole("button", { name: /attempt details/i });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(within(document.body).getByText("Attempt id")).toBeInTheDocument();
  });

  it("shows a not-found state for a missing case", async () => {
    vi.spyOn(api, "case").mockRejectedValue(new ApiError(404, "case 99 not found"));
    vi.spyOn(api, "timeline").mockRejectedValue(new ApiError(404, "nope"));
    renderCase("/cases/99");
    expect(await screen.findByText("No case #99")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /back to cases/i })).toBeInTheDocument();
  });

  it("surfaces the server's message when the service is unreachable", async () => {
    vi.spyOn(api, "case").mockRejectedValue(
      new ApiError(0, "Could not reach the recovery service. Anything shown may be stale."),
    );
    vi.spyOn(api, "timeline").mockRejectedValue(new ApiError(0, "unreachable"));
    renderCase();
    // The case query retries once on a transient failure before giving up.
    expect(await screen.findByRole("alert", {}, { timeout: 5000 })).toHaveTextContent(
      /could not reach the recovery service/i,
    );
  });

  it("still renders the case when the audit trail cannot be read", async () => {
    vi.spyOn(api, "case").mockResolvedValue(detail());
    vi.spyOn(api, "timeline").mockRejectedValue(new ApiError(500, "timeline down"));
    renderCase();
    // The investigation must not be blocked by one failed evidence source.
    expect(await screen.findByText("Recovery progress")).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Case #1" })).toBeInTheDocument();
  });

  it("explains a case that never opened an action", async () => {
    vi.spyOn(api, "case").mockResolvedValue(
      detail({ actions: [], attempts: [], provider_requests: [], verifications: [] }),
    );
    vi.spyOn(api, "timeline").mockResolvedValue(history());
    renderCase();
    expect(await screen.findByText(/no recovery action was ever opened/i)).toBeInTheDocument();
  });
});
