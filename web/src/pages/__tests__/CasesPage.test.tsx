import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Routes, Route } from "react-router-dom";
import { CasesPage } from "../CasesPage";
import { api, ApiError } from "@/lib/api";
import type { CasePage, CaseRow, Meta } from "@/lib/types";

const META: Meta = {
  environment: "reclaim_dev",
  case_states: ["AWAITING_CUSTOMER", "ESCALATED", "AMBIGUOUS", "VERIFIED_RECOVERED"],
  attention_states: ["ESCALATED", "AMBIGUOUS", "HALTED"],
  in_flight_states: ["EXECUTING", "RECONCILING", "AWAITING_CUSTOMER"],
  reviewable_actions: ["CREATE_PAYMENT_LINK"],
};

function row(over: Partial<CaseRow> = {}): CaseRow {
  return {
    case_id: 1, state: "AWAITING_CUSTOMER", amount_minor: 425000, currency: "INR",
    customer_ref: "cust_ravi_8812", anchor_kind: "ORDER", anchor_key: "ord_4101",
    attempt_count: 1, max_attempts: 2, recovered_amount_minor: 0,
    created_at: "2026-08-27T10:00:00+05:30", updated_at: "2026-08-27T10:05:00+05:30",
    has_pending_review: false, action_deadline_at: null, ...over,
  };
}

function page(rows: CaseRow[], total = rows.length): CasePage {
  return { rows, total, limit: 25, offset: 0 };
}

function renderPage(initial = "/cases") {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false }, mutations: { retry: false } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[initial]}>
        <Routes>
          <Route path="/cases" element={<CasesPage />} />
          <Route path="/cases/:caseId" element={<p>case detail</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.spyOn(api, "meta").mockResolvedValue(META);
});
afterEach(() => vi.restoreAllMocks());

describe("cases queue", () => {
  it("shows a skeleton before data arrives, not an empty state", async () => {
    vi.spyOn(api, "cases").mockReturnValue(new Promise(() => {}));
    const { container } = renderPage();
    await waitFor(() =>
      expect(container.querySelector('[aria-busy="true"]')).toBeInTheDocument(),
    );
    // An in-flight query must never be mistaken for "there is nothing here".
    expect(screen.queryByText(/no cases/i)).not.toBeInTheDocument();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });

  it("renders real case fields including money in minor units", async () => {
    vi.spyOn(api, "cases").mockResolvedValue(page([row()]));
    renderPage();
    expect(await screen.findByText("#1")).toBeInTheDocument();
    expect(screen.getByText("ord_4101")).toBeInTheDocument();
    expect(screen.getByText("cust_ravi_8812")).toBeInTheDocument();
    expect(screen.getByText("₹4,250.00")).toBeInTheDocument();
    expect(screen.getByText("Awaiting Customer")).toBeInTheDocument();
    expect(screen.getByText("1/2")).toBeInTheDocument();
  });

  it("marks cases needing a human and flags pending reviews", async () => {
    vi.spyOn(api, "cases").mockResolvedValue(
      page([
        row({ case_id: 5, state: "ESCALATED", has_pending_review: true }),
        row({ case_id: 6, state: "VERIFIED_RECOVERED" }),
      ]),
    );
    renderPage();
    const escalated = await screen.findByRole("link", { name: /case 5/i });
    expect(escalated).toHaveClass("is-attention");
    expect(within(escalated).getByText("Review")).toBeInTheDocument();

    const recovered = screen.getByRole("link", { name: /case 6/i });
    expect(recovered).not.toHaveClass("is-attention");
  });

  it("reads filters from the URL and sends them to the API", async () => {
    const spy = vi.spyOn(api, "cases").mockResolvedValue(page([]));
    renderPage("/cases?q=ravi&state=ESCALATED&attention=1&review=1&sort=amount&dir=asc&page=2");
    await waitFor(() => expect(spy).toHaveBeenCalled());
    expect(spy).toHaveBeenCalledWith(
      expect.objectContaining({
        q: "ravi",
        state: ["ESCALATED"],
        needs_attention: true,
        has_pending_review: true,
        sort: "amount",
        direction: "asc",
        limit: 25,
        offset: 25,
      }),
    );
  });

  it("puts a quick filter into the URL so the view can be shared", async () => {
    const spy = vi.spyOn(api, "cases").mockResolvedValue(page([row()]));
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("#1");
    await user.click(screen.getByRole("button", { name: /needs attention/i }));
    await waitFor(() =>
      expect(spy).toHaveBeenLastCalledWith(
        expect.objectContaining({ needs_attention: true }),
      ),
    );
  });

  it("toggles sort direction when the active column is clicked again", async () => {
    const spy = vi.spyOn(api, "cases").mockResolvedValue(page([row()]));
    const user = userEvent.setup();
    renderPage();
    await screen.findByText("#1");
    await user.click(screen.getByRole("button", { name: /amount/i }));
    await waitFor(() =>
      expect(spy).toHaveBeenLastCalledWith(
        expect.objectContaining({ sort: "amount", direction: "desc" }),
      ),
    );
    await user.click(screen.getByRole("button", { name: /amount/i }));
    await waitFor(() =>
      expect(spy).toHaveBeenLastCalledWith(
        expect.objectContaining({ sort: "amount", direction: "asc" }),
      ),
    );
  });

  it("navigates to the case when a row is activated by keyboard", async () => {
    vi.spyOn(api, "cases").mockResolvedValue(page([row({ case_id: 42 })]));
    const user = userEvent.setup();
    renderPage();
    const rowEl = await screen.findByRole("link", { name: /case 42/i });
    rowEl.focus();
    await user.keyboard("{Enter}");
    expect(await screen.findByText("case detail")).toBeInTheDocument();
  });

  it("distinguishes an empty result from an unfiltered empty queue", async () => {
    vi.spyOn(api, "cases").mockResolvedValue(page([]));
    renderPage("/cases?q=nothing");
    expect(await screen.findByText(/no cases match these filters/i)).toBeInTheDocument();
  });

  it("explains an empty queue when no filters are applied", async () => {
    vi.spyOn(api, "cases").mockResolvedValue(page([]));
    renderPage();
    expect(await screen.findByText("No cases yet")).toBeInTheDocument();
  });

  it("surfaces the server's own message on failure", async () => {
    vi.spyOn(api, "cases").mockRejectedValue(
      new ApiError(0, "Could not reach the recovery service. Anything shown may be stale."),
    );
    renderPage();
    expect(await screen.findByRole("alert")).toHaveTextContent(/could not reach the recovery service/i);
  });

  it("reports the result count for screen readers", async () => {
    vi.spyOn(api, "cases").mockResolvedValue(page([row()], 13));
    renderPage();
    await waitFor(() =>
      expect(screen.getByRole("status")).toHaveTextContent("13 cases"),
    );
  });

  it("hides pagination when a single page covers the result set", async () => {
    vi.spyOn(api, "cases").mockResolvedValue(page([row()], 1));
    renderPage();
    await screen.findByText("#1");
    expect(screen.queryByRole("navigation", { name: /pagination/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /next/i })).toBeDisabled();
  });
});
