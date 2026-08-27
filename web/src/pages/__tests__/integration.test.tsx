import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { act, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { RouterProvider, createMemoryRouter, Link, Outlet, useSearchParams } from "react-router-dom";
import { CasesPage } from "../CasesPage";
import { SystemPage } from "../SystemPage";
import { api, ApiError } from "@/lib/api";
import { CASE_PARAM } from "@/lib/routes";
import type { CasePage, CaseRow, Health, Meta, Overview, SystemStatus } from "@/lib/types";

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

const HEALTH: Health = { status: "ok", environment: "reclaim_dev" };

const SYSTEM: SystemStatus = {
  breaker: { state: "CLOSED", consecutive_failures: 0, opened_at: null, reset_after: null, trip_cause: null },
  leases_held: 0, leases_expired: 0, open_actions: 10,
  unresolved_attempts: 2, stale_writes_rejected: 0,
};

function overviewData(): Overview {
  const counts: Record<string, number> = Object.fromEntries(STATES.map((s) => [s, 0]));
  counts["ESCALATED"] = 3;
  return {
    state_counts: counts, attention_total: 5, in_flight_total: 3,
    recovered_count: 2, recovered_amount_minor: 554900, pending_reviews: 1,
    oldest_pending_review_at: null, breaker_state: "CLOSED",
    breaker_consecutive_failures: 0, recent_activity: [],
  };
}

function row(over: Partial<CaseRow> = {}): CaseRow {
  return {
    case_id: 6, state: "ESCALATED", amount_minor: 512000, currency: "INR",
    customer_ref: "cust_karan_7788", anchor_kind: "ORDER", anchor_key: "ord_4106",
    attempt_count: 2, max_attempts: 2, recovered_amount_minor: 0,
    created_at: "2026-08-27T21:14:59+05:30", updated_at: "2026-08-27T21:59:15+05:30",
    has_pending_review: false, action_deadline_at: null, ...over,
  };
}

const ALL = [row(), row({ case_id: 3, state: "AWAITING_CUSTOMER", anchor_key: "ord_4103", amount_minor: 78500 })];
const ATTENTION_ONLY = [row()];

/** Echoes the live query string so a test can read what the queue put there. */
function UrlProbe() {
  const [params] = useSearchParams();
  return <output data-testid="url">{params.toString()}</output>;
}

function Shell() {
  return (
    <>
      <UrlProbe />
      <Outlet />
    </>
  );
}

/**
 * A real router, not MemoryRouter: back/forward is part of what these tests
 * assert, and only a data router exposes the history stack to navigate.
 */
function renderApp(initial: string) {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 }, mutations: { retry: false } },
  });
  const router = createMemoryRouter(
    [
      {
        path: "/",
        element: <Shell />,
        children: [
          { index: true, element: <Link to="/cases">go to cases</Link> },
          { path: "cases", element: <CasesPage /> },
          { path: "cases/:caseId", element: <p>case detail</p> },
          { path: "system", element: <SystemPage /> },
          { path: "reviews", element: <p>reviews</p> },
        ],
      },
    ],
    { initialEntries: [initial] },
  );
  const result = render(
    <QueryClientProvider client={client}>
      <RouterProvider router={router} />
    </QueryClientProvider>,
  );
  return { ...result, router };
}

beforeEach(() => {
  vi.spyOn(api, "meta").mockResolvedValue(META);
  vi.spyOn(api, "health").mockResolvedValue(HEALTH);
  vi.spyOn(api, "system").mockResolvedValue(SYSTEM);
  vi.spyOn(api, "overview").mockResolvedValue(overviewData());
  vi.spyOn(api, "cases").mockImplementation(async (q = {}) => {
    const rows = q.needs_attention ? ATTENTION_ONLY : ALL;
    return { rows, total: rows.length, limit: 25, offset: 0 } as CasePage;
  });
});
afterEach(() => vi.restoreAllMocks());

describe("queue filters live in the URL", () => {
  it("applies a filter opened directly by URL, so a shared link shows what the sender saw", async () => {
    renderApp("/cases?attention=1");
    await waitFor(() => expect(screen.getByText("ord_4106")).toBeInTheDocument());
    expect(screen.queryByText("ord_4103")).not.toBeInTheDocument();
    expect(api.cases).toHaveBeenCalledWith(expect.objectContaining({ needs_attention: true }));
  });

  it("writes a toggled filter into the URL", async () => {
    const user = userEvent.setup();
    renderApp("/cases");
    await waitFor(() => expect(screen.getByText("ord_4103")).toBeInTheDocument());
    await user.click(screen.getByRole("button", { name: /needs attention/i }));
    await waitFor(() =>
      expect(screen.getByTestId("url").textContent).toContain(`${CASE_PARAM.attention}=1`),
    );
  });

  it("lets Back undo a filter instead of leaving the queue", async () => {
    const user = userEvent.setup();
    const { router } = renderApp("/cases");
    await waitFor(() => expect(screen.getByText("ord_4103")).toBeInTheDocument());

    await user.click(screen.getByRole("button", { name: /needs attention/i }));
    await waitFor(() => expect(screen.getByTestId("url").textContent).toContain("attention=1"));

    await act(() => router.navigate(-1));

    // The operator lands back on the unfiltered queue, still on the queue.
    await waitFor(() => expect(screen.getByTestId("url").textContent).toBe(""));
    expect(await screen.findByText("ord_4103")).toBeInTheDocument();
  });
});

describe("navigation between surfaces", () => {
  it("routes the System view into the queue with a filter the queue honours", async () => {
    const user = userEvent.setup();
    renderApp("/system");
    const nav = await screen.findByRole("navigation", { name: "Operational surfaces" });
    await user.click(within(nav).getByRole("link", { name: "Cases needing a human" }));

    // The link is only useful if the queue actually narrows.
    await waitFor(() => expect(screen.getByText("ord_4106")).toBeInTheDocument());
    expect(screen.queryByText("ord_4103")).not.toBeInTheDocument();
    expect(api.cases).toHaveBeenCalledWith(expect.objectContaining({ needs_attention: true }));
  });
});

describe("an unavailable service is never rendered as an empty estate", () => {
  it("does not show zero cases when the queue query fails", async () => {
    vi.spyOn(api, "cases").mockRejectedValue(new ApiError(0, "Could not reach the recovery service."));
    renderApp("/cases");
    expect(await screen.findByRole("alert")).toBeInTheDocument();
    expect(screen.queryByText(/no cases match/i)).not.toBeInTheDocument();
    expect(screen.queryByText("0 cases")).not.toBeInTheDocument();
  });
});
