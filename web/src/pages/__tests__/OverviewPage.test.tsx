import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { OverviewPage } from "../OverviewPage";
import { api, ApiError } from "@/lib/api";
import type { CasePage, CaseRow, Meta, Overview, ReviewQueue, SystemStatus } from "@/lib/types";

const STATES = [
  "NEW", "ENRICHING", "DIAGNOSING", "POLICY_EVAL", "ACTION_READY", "EXECUTING",
  "AWAITING_CUSTOMER", "ATTEMPT_FAILED", "AMBIGUOUS", "RECONCILING", "HALTED",
  "ESCALATED", "VERIFIED_RECOVERED", "VERIFIED_FAILED", "EXPIRED_UNRESOLVED",
];

const META: Meta = {
  environment: "reclaim_dev", case_states: STATES,
  attention_states: ["ESCALATED", "AMBIGUOUS", "HALTED"],
  in_flight_states: ["EXECUTING", "RECONCILING", "AWAITING_CUSTOMER"],
  reviewable_actions: ["CREATE_PAYMENT_LINK"],
};

function overview(over: Partial<Overview> = {}): Overview {
  const counts: Record<string, number> = Object.fromEntries(STATES.map((s) => [s, 0]));
  counts["ESCALATED"] = 3; counts["AMBIGUOUS"] = 1; counts["HALTED"] = 1;
  counts["AWAITING_CUSTOMER"] = 3; counts["VERIFIED_RECOVERED"] = 2;
  return {
    state_counts: counts, attention_total: 5, in_flight_total: 3,
    recovered_count: 2, recovered_amount_minor: 554900, pending_reviews: 2,
    oldest_pending_review_at: new Date(Date.now() - 600_000).toISOString(),
    breaker_state: "CLOSED", breaker_consecutive_failures: 0,
    recent_activity: [
      { id: 99, occurred_at: new Date(Date.now() - 60_000).toISOString(),
        event_type: "state_transition", case_id: 13, reason_code: "breaker_open",
        prev_state: "ACTION_READY", new_state: "HALTED", worker_id: "executor", reviewer_ref: null },
      { id: 98, occurred_at: new Date(Date.now() - 90_000).toISOString(),
        event_type: "lease_claimed", case_id: 13, reason_code: "lease_claimed",
        prev_state: "ACTION_READY", new_state: "ACTION_READY", worker_id: "executor", reviewer_ref: null },
    ],
    ...over,
  };
}

function caseRow(over: Partial<CaseRow> = {}): CaseRow {
  return {
    case_id: 6, state: "ESCALATED", amount_minor: 512000, currency: "INR",
    customer_ref: "cust_karan_7788", anchor_kind: "ORDER", anchor_key: "ord_4106",
    attempt_count: 2, max_attempts: 2, recovered_amount_minor: 0,
    created_at: new Date(Date.now() - 7_200_000).toISOString(),
    updated_at: new Date(Date.now() - 600_000).toISOString(),
    has_pending_review: true, action_deadline_at: null, ...over,
  };
}

const SYSTEM: SystemStatus = {
  breaker: { state: "CLOSED", consecutive_failures: 0, opened_at: null, reset_after: null, trip_cause: null },
  leases_held: 0, leases_expired: 0, open_actions: 10,
  unresolved_attempts: 2, stale_writes_rejected: 0,
};

function casePage(rows: CaseRow[], total = rows.length): CasePage {
  return { rows, total, limit: 5, offset: 0 };
}

function reviewQueue(n: number): ReviewQueue {
  return {
    rows: Array.from({ length: n }, (_, i) => ({
      review_id: i + 1, case_id: 5 + i, status: "PENDING", reviewer_ref: null,
      selected_action: null,
      review_expires_at: new Date(Date.now() + 20 * 3_600_000).toISOString(),
      created_at: new Date(Date.now() - 600_000).toISOString(), decided_at: null,
      case_state: "ESCALATED", amount_minor: 249000, currency: "INR",
      customer_ref: "cust_sana_3140", anchor_key: "ord_4105",
    })),
    total: n, limit: 4, offset: 0, status: "PENDING",
  };
}

function show() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/"]}>
        <Routes>
          <Route path="/" element={<OverviewPage />} />
          <Route path="/cases" element={<p>cases queue</p>} />
          <Route path="/cases/:caseId" element={<p>case investigation</p>} />
          <Route path="/reviews" element={<p>review queue</p>} />
          <Route path="/reviews/:caseId" element={<p>review workspace</p>} />
          <Route path="/system" element={<p>system</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.spyOn(api, "meta").mockResolvedValue(META);
  vi.spyOn(api, "system").mockResolvedValue(SYSTEM);
});
afterEach(() => vi.restoreAllMocks());

describe("operations overview", () => {
  it("shows a structural skeleton while loading, not zeroes", async () => {
    vi.spyOn(api, "overview").mockReturnValue(new Promise(() => {}));
    vi.spyOn(api, "cases").mockResolvedValue(casePage([]));
    vi.spyOn(api, "reviews").mockResolvedValue(reviewQueue(0));
    const { container } = show();
    await waitFor(() =>
      expect(container.querySelector('[aria-busy="true"]')).toBeInTheDocument(),
    );
    expect(screen.queryByText("Needs attention")).not.toBeInTheDocument();
  });

  it("renders the real operational metrics", async () => {
    vi.spyOn(api, "overview").mockResolvedValue(overview());
    vi.spyOn(api, "cases").mockResolvedValue(casePage([caseRow()]));
    vi.spyOn(api, "reviews").mockResolvedValue(reviewQueue(2));
    show();
    // Labels are uppercased by CSS; scoped to the band because the same words
    // also title the sections below it.
    const band = within(await screen.findByRole("list", { name: "Operational summary" }));
    expect(band.getByText("Needs attention")).toBeInTheDocument();
    expect(band.getByText("5")).toBeInTheDocument();
    expect(band.getByText("In flight")).toBeInTheDocument();
    expect(band.getByText("3")).toBeInTheDocument();
  });

  it("counts recovered cases without asserting a currency the API does not supply", async () => {
    vi.spyOn(api, "overview").mockResolvedValue(overview());
    vi.spyOn(api, "cases").mockResolvedValue(casePage([caseRow()]));
    vi.spyOn(api, "reviews").mockResolvedValue(reviewQueue(0));
    show();
    const band = within(await screen.findByRole("list", { name: "Operational summary" }));
    expect(band.getByText("Recovered")).toBeInTheDocument();
    // The overview endpoint returns a bare minor-unit total with no currency,
    // so no monetary figure is rendered from it.
    expect(screen.queryByText("₹5,549.00")).not.toBeInTheDocument();
  });

  it("formats attention-case amounts with their own currency", async () => {
    vi.spyOn(api, "overview").mockResolvedValue(overview());
    vi.spyOn(api, "cases").mockResolvedValue(casePage([caseRow()]));
    vi.spyOn(api, "reviews").mockResolvedValue(reviewQueue(0));
    show();
    expect(await screen.findByText("₹5,120.00")).toBeInTheDocument();
  });

  it("lists cases needing attention with the state's operational meaning", async () => {
    vi.spyOn(api, "overview").mockResolvedValue(overview());
    vi.spyOn(api, "cases").mockResolvedValue(casePage([
      caseRow(), caseRow({ case_id: 7, state: "AMBIGUOUS", amount_minor: 96000, has_pending_review: false }),
    ]));
    vi.spyOn(api, "reviews").mockResolvedValue(reviewQueue(0));
    show();
    expect(await screen.findByText(/Money may or may not have moved/)).toBeInTheDocument();
    // "Ambiguous" also labels a row in the state distribution.
    expect(screen.getAllByText("Ambiguous").length).toBeGreaterThan(0);
  });

  it("shows every non-zero state and reports how many are empty", async () => {
    vi.spyOn(api, "overview").mockResolvedValue(overview());
    vi.spyOn(api, "cases").mockResolvedValue(casePage([]));
    vi.spyOn(api, "reviews").mockResolvedValue(reviewQueue(0));
    show();
    expect(await screen.findByText("AWAITING_CUSTOMER")).toBeInTheDocument();
    // Materially different outcomes stay separate.
    expect(screen.getByText("AMBIGUOUS")).toBeInTheDocument();
    expect(screen.getByText("VERIFIED_RECOVERED")).toBeInTheDocument();
    expect(screen.getByText(/10 of 15 states have no cases/)).toBeInTheDocument();
  });

  it("treats an empty attention list as a healthy state", async () => {
    vi.spyOn(api, "overview").mockResolvedValue(overview({ attention_total: 0 }));
    vi.spyOn(api, "cases").mockResolvedValue(casePage([]));
    vi.spyOn(api, "reviews").mockResolvedValue(reviewQueue(0));
    show();
    expect(await screen.findByText("Nothing needs attention")).toBeInTheDocument();
  });

  it("treats an empty review queue as normal, not broken", async () => {
    vi.spyOn(api, "overview").mockResolvedValue(overview({ pending_reviews: 0, oldest_pending_review_at: null }));
    vi.spyOn(api, "cases").mockResolvedValue(casePage([]));
    vi.spyOn(api, "reviews").mockResolvedValue(reviewQueue(0));
    show();
    expect(await screen.findByText("No cases are waiting on a reviewer")).toBeInTheDocument();
    expect(screen.getByText(/normal, healthy state/)).toBeInTheDocument();
  });

  it("omits routine lease bookkeeping from recent activity", async () => {
    vi.spyOn(api, "overview").mockResolvedValue(overview());
    vi.spyOn(api, "cases").mockResolvedValue(casePage([]));
    vi.spyOn(api, "reviews").mockResolvedValue(reviewQueue(0));
    show();
    expect(await screen.findByText("State changed")).toBeInTheDocument();
    expect(screen.queryByText("Lease claimed")).not.toBeInTheDocument();
  });

  it("reports the environment and breaker state from the API", async () => {
    vi.spyOn(api, "overview").mockResolvedValue(overview());
    vi.spyOn(api, "cases").mockResolvedValue(casePage([]));
    vi.spyOn(api, "reviews").mockResolvedValue(reviewQueue(0));
    show();
    expect(await screen.findByText("reclaim_dev")).toBeInTheDocument();
    expect(screen.getByText("CLOSED")).toBeInTheDocument();
    expect(screen.getByText("Responding")).toBeInTheDocument();
  });

  it("warns when the breaker is open", async () => {
    vi.spyOn(api, "overview").mockResolvedValue(
      overview({ breaker_state: "OPEN", breaker_consecutive_failures: 5 }),
    );
    vi.spyOn(api, "cases").mockResolvedValue(casePage([]));
    vi.spyOn(api, "reviews").mockResolvedValue(reviewQueue(0));
    show();
    expect(await screen.findByText(/Dispatch is stopped/)).toBeInTheDocument();
  });

  it("distinguishes an unavailable API from an empty system", async () => {
    vi.spyOn(api, "overview").mockRejectedValue(
      new ApiError(0, "Could not reach the recovery service. Anything shown may be stale."),
    );
    vi.spyOn(api, "cases").mockResolvedValue(casePage([]));
    vi.spyOn(api, "reviews").mockResolvedValue(reviewQueue(0));
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent(/could not reach the recovery service/i);
    expect(screen.getByText(/This is not the same as an empty system/)).toBeInTheDocument();
    // No zero-valued metrics are shown in place of unknown ones.
    expect(screen.queryByRole("list", { name: "Operational summary" })).not.toBeInTheDocument();
  });

  it("keeps the dashboard usable when a secondary panel fails", async () => {
    vi.spyOn(api, "overview").mockResolvedValue(overview());
    vi.spyOn(api, "cases").mockRejectedValue(new ApiError(500, "cases unavailable"));
    vi.spyOn(api, "reviews").mockResolvedValue(reviewQueue(0));
    show();
    const band = within(await screen.findByRole("list", { name: "Operational summary" }));
    expect(band.getByText("Needs attention")).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent(/cases unavailable/i);
  });

  it("navigates into a case from the attention list", async () => {
    vi.spyOn(api, "overview").mockResolvedValue(overview());
    vi.spyOn(api, "cases").mockResolvedValue(casePage([caseRow()]));
    vi.spyOn(api, "reviews").mockResolvedValue(reviewQueue(0));
    const user = userEvent.setup();
    show();
    const row = await screen.findByRole("link", { name: /case 6/i });
    row.focus();
    await user.keyboard("{Enter}");
    expect(await screen.findByText("case investigation")).toBeInTheDocument();
  });

  it("navigates into the review workspace from the review list", async () => {
    vi.spyOn(api, "overview").mockResolvedValue(overview());
    vi.spyOn(api, "cases").mockResolvedValue(casePage([]));
    vi.spyOn(api, "reviews").mockResolvedValue(reviewQueue(1));
    const user = userEvent.setup();
    show();
    await user.click(await screen.findByRole("link", { name: /#5/ }));
    expect(await screen.findByText("review workspace")).toBeInTheDocument();
  });

  it("links each metric to where it is acted on", async () => {
    vi.spyOn(api, "overview").mockResolvedValue(overview());
    vi.spyOn(api, "cases").mockResolvedValue(casePage([]));
    vi.spyOn(api, "reviews").mockResolvedValue(reviewQueue(0));
    show();
    const attention = await screen.findByRole("link", { name: /needs attention/i });
    expect(attention).toHaveAttribute("href", "/cases?attention=1");
    expect(screen.getByRole("link", { name: /awaiting review/i })).toHaveAttribute("href", "/reviews");
  });
});
