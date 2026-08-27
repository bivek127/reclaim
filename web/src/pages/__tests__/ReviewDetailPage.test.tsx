import { describe, expect, it, vi, afterEach, beforeEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { ReviewDetailPage } from "../ReviewDetailPage";
import { api, ApiError } from "@/lib/api";
import type { CaseDetail, HumanReview } from "@/lib/types";

const HOUR = 3_600_000;

function review(over: Partial<HumanReview> = {}): HumanReview {
  return {
    id: 2, status: "PENDING", reviewer_ref: null, selected_action: null,
    review_expires_at: new Date(Date.now() + 20 * HOUR).toISOString(),
    created_at: new Date(Date.now() - HOUR).toISOString(), decided_at: null, ...over,
  };
}

function detail(over: Partial<CaseDetail> = {}): CaseDetail {
  return {
    case: {
      case_id: 6, state: "ESCALATED", amount_minor: 512000, currency: "INR",
      customer_ref: "cust_karan_7788", anchor_kind: "ORDER", anchor_key: "ord_4106",
      attempt_count: 2, max_attempts: 2, recovered_amount_minor: 0,
      created_at: new Date(Date.now() - 2 * HOUR).toISOString(),
      updated_at: new Date(Date.now() - HOUR).toISOString(),
      has_pending_review: true, action_deadline_at: null,
    },
    obligation: {
      id: 6, anchor_kind: "ORDER", anchor_key: "ord_4106",
      anchor_canonical: "order:ord_4106", amount_minor: 512000, currency: "INR",
      customer_ref: "cust_karan_7788", source_event_id: "evt_ord_4106",
      first_seen_at: new Date(Date.now() - 2 * HOUR).toISOString(),
      last_seen_at: new Date(Date.now() - 2 * HOUR).toISOString(),
    },
    diagnoses: [{
      id: 6, source: "DETERMINISTIC_FALLBACK", model: null, model_version: null,
      prompt_version: "v1", cause: "UNKNOWN", recommended_action: "CREATE_PAYMENT_LINK",
      reasoning: "deterministic fallback", confidence: null, llm_retry_count: 0,
      created_at: new Date(Date.now() - 2 * HOUR).toISOString(),
    }],
    policy_decisions: [
      { id: 7, diagnosis_id: 6, policy_version: "1.0", lookup_miss: false,
        conflicting_history: false, ambiguity_signal: false, verdict: "ALLOW",
        selected_action: "CREATE_PAYMENT_LINK", reason_code: "policy_allow_create_link",
        created_at: new Date(Date.now() - 2 * HOUR).toISOString() },
      { id: 9, diagnosis_id: 6, policy_version: "1.0", lookup_miss: false,
        conflicting_history: false, ambiguity_signal: false, verdict: "ESCALATE",
        selected_action: null, reason_code: "policy_escalate_budget",
        created_at: new Date(Date.now() - HOUR).toISOString() },
    ],
    actions: [{
      id: 6, action_type: "CREATE_PAYMENT_LINK", status: "TERMINAL_FAILED",
      sequence_no: 1, policy_decision_id: 7, superseded_by: null,
      provider_expires_at: null, action_deadline_at: null,
      created_at: new Date(Date.now() - 2 * HOUR).toISOString(),
      resolved_at: new Date(Date.now() - 2 * HOUR).toISOString(),
    }],
    attempts: [{
      id: 6, action_id: 6, attempt_no: 1, idempotency_key: "rcv_KARAN1",
      provider_reference: "rcv_KARAN1", state: "REJECTED", amount_minor: 512000,
      currency: "INR", created_at: new Date(Date.now() - 2 * HOUR).toISOString(),
      settled_at: new Date(Date.now() - 2 * HOUR).toISOString(),
    }],
    provider_requests: [{
      id: 6, attempt_id: 6, operation: "create_payment_link", request_no: 1,
      idempotency_key: "rcv_KARAN1", outcome: "REJECTED", http_status: null,
      provider_correlation_id: null,
      sent_at: new Date(Date.now() - 2 * HOUR).toISOString(),
      completed_at: new Date(Date.now() - 2 * HOUR).toISOString(), response_body: null,
    }],
    verifications: [],
    reviews: [review()],
    ...over,
  };
}

const EVIDENCE = {
  reviewable_actions: ["CREATE_PAYMENT_LINK"],
  failure_codes: ["REJECTED"],
};

function show(path = "/reviews/6") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/reviews/:caseId" element={<ReviewDetailPage />} />
          <Route path="/reviews" element={<p>queue</p>} />
          <Route path="/cases/:caseId" element={<p>investigation</p>} />
          <Route path="/cases/:caseId/timeline" element={<p>timeline</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

async function identify(user: ReturnType<typeof userEvent.setup>) {
  await user.type(await screen.findByLabelText(/your reviewer reference/i), "ops@x.com");
}

beforeEach(() => {
  window.localStorage.clear();
  vi.spyOn(api, "reviewEvidence").mockResolvedValue(EVIDENCE);
});
afterEach(() => vi.restoreAllMocks());

describe("review workspace", () => {
  it("explains why the case reached a human", async () => {
    vi.spyOn(api, "case").mockResolvedValue(detail());
    show();
    expect(await screen.findByText(/every permitted attempt was used/i)).toBeInTheDocument();
    // Appears both in the escalation summary and in the policy trail below it.
    expect(screen.getAllByText("policy_escalate_budget").length).toBeGreaterThan(0);
    expect(screen.getByText("2 of 2")).toBeInTheDocument();
  });

  it("states that approval proposes an action rather than moving money", async () => {
    vi.spyOn(api, "case").mockResolvedValue(detail());
    show();
    expect(await screen.findByText(/not a payment/i)).toBeInTheDocument();
    expect(screen.getByText(/The executor picks it up/i)).toBeInTheDocument();
  });

  it("keeps advisory diagnosis separate from authoritative policy", async () => {
    vi.spyOn(api, "case").mockResolvedValue(detail());
    show();
    expect(await screen.findByText(/it authorises nothing/i)).toBeInTheDocument();
    expect(screen.getByText(/This is what authorised or refused/i)).toBeInTheDocument();
  });

  it("does not present a provider claim as verification", async () => {
    vi.spyOn(api, "case").mockResolvedValue(detail());
    show();
    expect(await screen.findByText("Not verified")).toBeInTheDocument();
    expect(screen.getByText(/two independently sourced pieces of evidence agree/i)).toBeInTheDocument();
  });

  it("offers only actions the executor can dispatch", async () => {
    vi.spyOn(api, "case").mockResolvedValue(detail());
    show();
    expect(await screen.findByRole("radio", { name: /CREATE_PAYMENT_LINK/ })).toBeInTheDocument();
    expect(screen.queryByRole("radio", { name: /RETRY_CHARGE/ })).not.toBeInTheDocument();
  });

  it("requires a reviewer reference before a decision can be made", async () => {
    vi.spyOn(api, "case").mockResolvedValue(detail());
    show();
    expect(await screen.findByRole("button", { name: "Approve" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Reject" })).toBeDisabled();
    expect(screen.getByText(/Enter your reviewer reference/i)).toBeInTheDocument();
  });

  it("says the reviewer reference is a label, not authentication", async () => {
    vi.spyOn(api, "case").mockResolvedValue(detail());
    show();
    expect(await screen.findByText(/not a sign-in/i)).toBeInTheDocument();
  });

  it("spells out the consequence before approving, and only then submits", async () => {
    vi.spyOn(api, "case").mockResolvedValue(detail());
    const approve = vi.spyOn(api, "approve").mockResolvedValue({ applied: true });
    const user = userEvent.setup();
    show();
    await identify(user);
    await user.click(screen.getByRole("button", { name: "Approve" }));

    expect(screen.getByText("Approve this recovery?")).toBeInTheDocument();
    expect(screen.getByText(/No money moves now\./)).toBeInTheDocument();
    expect(approve).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: /yes, propose/i }));
    await waitFor(() =>
      expect(approve).toHaveBeenCalledWith(6, "ops@x.com", "CREATE_PAYMENT_LINK"),
    );
  });

  it("spells out that rejection closes the case as not recovered", async () => {
    vi.spyOn(api, "case").mockResolvedValue(detail());
    const reject = vi.spyOn(api, "reject").mockResolvedValue({ applied: true });
    const user = userEvent.setup();
    show();
    await identify(user);
    await user.click(screen.getByRole("button", { name: "Reject" }));
    expect(screen.getByText("Reject this recovery?")).toBeInTheDocument();
    expect(screen.getByText("VERIFIED_FAILED")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /yes, reject and close/i }));
    await waitFor(() => expect(reject).toHaveBeenCalledWith(6, "ops@x.com"));
  });

  it("lets the reviewer cancel without submitting", async () => {
    vi.spyOn(api, "case").mockResolvedValue(detail());
    const approve = vi.spyOn(api, "approve").mockResolvedValue({ applied: true });
    const user = userEvent.setup();
    show();
    await identify(user);
    await user.click(screen.getByRole("button", { name: "Approve" }));
    await user.click(screen.getByRole("button", { name: /cancel/i }));
    expect(screen.getByRole("button", { name: "Approve" })).toBeInTheDocument();
    expect(approve).not.toHaveBeenCalled();
  });

  it("blocks a double submission while a decision is in flight", async () => {
    vi.spyOn(api, "case").mockResolvedValue(detail());
    const approve = vi.spyOn(api, "approve").mockReturnValue(new Promise(() => {}));
    const user = userEvent.setup();
    show();
    await identify(user);
    await user.click(screen.getByRole("button", { name: "Approve" }));
    const confirm = screen.getByRole("button", { name: /yes, propose/i });
    await user.click(confirm);
    await waitFor(() => expect(screen.getByRole("button", { name: /submitting/i })).toBeDisabled());
    await user.click(screen.getByRole("button", { name: /submitting/i }));
    expect(approve).toHaveBeenCalledTimes(1);
  });

  it("surfaces a concurrency conflict without claiming success", async () => {
    vi.spyOn(api, "case").mockResolvedValue(detail());
    vi.spyOn(api, "approve").mockRejectedValue(
      new ApiError(409, "This case is not awaiting review, or another worker holds it."),
    );
    const user = userEvent.setup();
    show();
    await identify(user);
    await user.click(screen.getByRole("button", { name: "Approve" }));
    await user.click(screen.getByRole("button", { name: /yes, propose/i }));

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("This case changed");
    expect(alert).toHaveTextContent(/not awaiting review/i);
    // The case is not locally mutated into the desired outcome.
    expect(screen.queryByText("APPROVED")).not.toBeInTheDocument();
    expect(within(alert).getByRole("button", { name: /reload this review/i })).toBeInTheDocument();
  });

  it("reports a non-conflict failure distinctly", async () => {
    vi.spyOn(api, "case").mockResolvedValue(detail());
    vi.spyOn(api, "approve").mockRejectedValue(new ApiError(0, "service unreachable"));
    const user = userEvent.setup();
    show();
    await identify(user);
    await user.click(screen.getByRole("button", { name: "Approve" }));
    await user.click(screen.getByRole("button", { name: /yes, propose/i }));
    expect(await screen.findByText("The decision was not applied")).toBeInTheDocument();
  });

  it("warns when the deadline has lapsed but still allows a decision", async () => {
    vi.spyOn(api, "case").mockResolvedValue(detail({
      reviews: [review({ review_expires_at: new Date(Date.now() - HOUR).toISOString() })],
    }));
    const user = userEvent.setup();
    show();
    expect(await screen.findByText("Deadline passed")).toBeInTheDocument();
    // The backend still accepts a decision while the row is PENDING, so the UI
    // must not pretend otherwise.
    await identify(user);
    expect(screen.getByRole("button", { name: "Approve" })).toBeEnabled();
    expect(screen.getByText(/may close it at any moment/i)).toBeInTheDocument();
  });

  it("offers no decision once the review is closed", async () => {
    vi.spyOn(api, "case").mockResolvedValue(detail({
      reviews: [review({
        status: "APPROVED", reviewer_ref: "ops.reviewer@reclaim.local",
        selected_action: "CREATE_PAYMENT_LINK",
        decided_at: new Date(Date.now() - 60_000).toISOString(),
      })],
    }));
    show();
    expect(await screen.findByText("This review is closed. Any further change to the case happens through the normal recovery path, not through this screen.")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
    expect(screen.getByText("ops.reviewer@reclaim.local")).toBeInTheDocument();
  });

  it("shows an expired review as closed", async () => {
    vi.spyOn(api, "case").mockResolvedValue(detail({
      reviews: [review({ status: "EXPIRED" })],
    }));
    show();
    await screen.findByText(/This review is closed/);
    expect(screen.queryByRole("button", { name: "Approve" })).not.toBeInTheDocument();
  });

  it("notes when an action has already been proposed", async () => {
    const d = detail();
    vi.spyOn(api, "case").mockResolvedValue({
      ...d,
      actions: [...d.actions, {
        ...d.actions[0]!, id: 16, status: "PROPOSED", sequence_no: 2, resolved_at: null,
      }],
    });
    show();
    expect(await screen.findByText(/Already proposed/)).toBeInTheDocument();
  });

  it("routes to the investigation and the audit trail", async () => {
    vi.spyOn(api, "case").mockResolvedValue(detail());
    show();
    expect(await screen.findByRole("link", { name: /full investigation/i }))
      .toHaveAttribute("href", "/cases/6");
    expect(screen.getByRole("link", { name: /events & logs/i }))
      .toHaveAttribute("href", "/cases/6/timeline");
  });

  it("explains a case that was never escalated", async () => {
    vi.spyOn(api, "case").mockResolvedValue(detail({ reviews: [] }));
    show();
    expect(await screen.findByText("This case has never been escalated")).toBeInTheDocument();
  });

  it("shows a not-found state for a missing case", async () => {
    vi.spyOn(api, "case").mockRejectedValue(new ApiError(404, "case 99 not found"));
    show("/reviews/99");
    expect(await screen.findByText("No case #99")).toBeInTheDocument();
  });

  it("renders the workspace when optional evidence is missing", async () => {
    vi.spyOn(api, "reviewEvidence").mockRejectedValue(new ApiError(500, "evidence down"));
    vi.spyOn(api, "case").mockResolvedValue(detail({ diagnoses: [], verifications: [] }));
    show();
    // Evidence gaps must not block the reviewer from seeing the rest.
    expect(await screen.findByText(/every permitted attempt was used/i)).toBeInTheDocument();
    expect(screen.getByText(/No diagnosis was recorded/)).toBeInTheDocument();
  });
});
