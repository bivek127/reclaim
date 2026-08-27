import { describe, expect, it, vi, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { ReviewsPage } from "../ReviewsPage";
import { api, ApiError } from "@/lib/api";
import type { ReviewQueue, ReviewQueueRow } from "@/lib/types";

function row(over: Partial<ReviewQueueRow> = {}): ReviewQueueRow {
  return {
    review_id: 1, case_id: 5, status: "PENDING", reviewer_ref: null,
    selected_action: null,
    review_expires_at: new Date(Date.now() + 3_600_000).toISOString(),
    created_at: new Date(Date.now() - 600_000).toISOString(), decided_at: null,
    case_state: "ESCALATED", amount_minor: 249000, currency: "INR",
    customer_ref: "cust_sana_3140", anchor_key: "ord_4105", ...over,
  };
}

function queue(rows: ReviewQueueRow[], status = "PENDING"): ReviewQueue {
  return { rows, total: rows.length, limit: 100, offset: 0, status };
}

function show(path = "/reviews") {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={[path]}>
        <Routes>
          <Route path="/reviews" element={<ReviewsPage />} />
          <Route path="/reviews/:caseId" element={<p>review workspace</p>} />
          <Route path="/cases" element={<p>cases</p>} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.restoreAllMocks());

describe("review queue", () => {
  it("shows pending reviews with their financial context", async () => {
    vi.spyOn(api, "reviews").mockResolvedValue(queue([row()]));
    show();
    expect(await screen.findByText("#5")).toBeInTheDocument();
    expect(screen.getByText("₹2,490.00")).toBeInTheDocument();
    expect(screen.getByText("cust_sana_3140")).toBeInTheDocument();
    expect(screen.getByText("Escalated")).toBeInTheDocument();
  });

  it("marks a review whose deadline has passed", async () => {
    vi.spyOn(api, "reviews").mockResolvedValue(queue([
      row({ review_expires_at: new Date(Date.now() - 3_600_000).toISOString() }),
    ]));
    show();
    expect(await screen.findByText("deadline passed")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /review case 5/i })).toHaveClass("is-attention");
  });

  it("shows a skeleton while loading rather than an empty queue", async () => {
    vi.spyOn(api, "reviews").mockReturnValue(new Promise(() => {}));
    const { container } = show();
    await waitFor(() =>
      expect(container.querySelector('[aria-busy="true"]')).toBeInTheDocument(),
    );
    expect(screen.queryByText(/nothing is waiting/i)).not.toBeInTheDocument();
  });

  it("never turns an API failure into an empty queue", async () => {
    vi.spyOn(api, "reviews").mockRejectedValue(new ApiError(0, "service unreachable"));
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent(/service unreachable/i);
    expect(screen.queryByText(/nothing is waiting/i)).not.toBeInTheDocument();
  });

  it("explains an empty pending queue", async () => {
    vi.spyOn(api, "reviews").mockResolvedValue(queue([]));
    show();
    expect(await screen.findByText("Nothing is waiting on a reviewer")).toBeInTheDocument();
  });

  it("switches status and requests that status from the API", async () => {
    const spy = vi.spyOn(api, "reviews").mockResolvedValue(queue([]));
    const user = userEvent.setup();
    show();
    await screen.findByText("Nothing is waiting on a reviewer");
    await user.click(screen.getByRole("tab", { name: /approved/i }));
    await waitFor(() => expect(spy).toHaveBeenLastCalledWith("APPROVED", 100, 0));
  });

  it("shows who decided a closed review", async () => {
    vi.spyOn(api, "reviews").mockResolvedValue(queue([
      row({
        status: "APPROVED", reviewer_ref: "ops.reviewer@reclaim.local",
        decided_at: new Date(Date.now() - 60_000).toISOString(),
      }),
    ], "APPROVED"));
    show();
    expect(await screen.findByText(/ops\.reviewer@reclaim\.local/)).toBeInTheDocument();
  });

  it("opens the review workspace from a row", async () => {
    vi.spyOn(api, "reviews").mockResolvedValue(queue([row()]));
    const user = userEvent.setup();
    show();
    const link = await screen.findByRole("link", { name: /review case 5/i });
    link.focus();
    await user.keyboard("{Enter}");
    expect(await screen.findByText("review workspace")).toBeInTheDocument();
  });
});
