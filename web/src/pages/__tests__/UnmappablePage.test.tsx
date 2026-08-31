import { describe, expect, it, vi, afterEach } from "vitest";
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter, Route, Routes } from "react-router-dom";
import { UnmappablePage } from "../UnmappablePage";
import { api, ApiError } from "@/lib/api";
import type { UnmappableQueue, UnmappableWebhookRow } from "@/lib/types";

function row(over: Partial<UnmappableWebhookRow> = {}): UnmappableWebhookRow {
  return {
    webhook_event_id: 1,
    provider_event_id: "evt_9f3a",
    event_type: "subscription.pending",
    payload: { foo: "bar" },
    received_at: new Date(Date.now() - 600_000).toISOString(),
    ...over,
  };
}

function queue(rows: UnmappableWebhookRow[]): UnmappableQueue {
  return { rows, total: rows.length, limit: 100, offset: 0 };
}

function show() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter initialEntries={["/unmappable"]}>
        <Routes>
          <Route path="/unmappable" element={<UnmappablePage />} />
        </Routes>
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

afterEach(() => vi.restoreAllMocks());

describe("unmapped webhook queue", () => {
  it("lists an unmapped webhook by event type, provider id, and received time", async () => {
    vi.spyOn(api, "unmappable").mockResolvedValue(queue([row()]));
    show();
    expect(await screen.findByText("subscription.pending")).toBeInTheDocument();
    expect(screen.getByText("evt_9f3a")).toBeInTheDocument();
  });

  it("shows a skeleton while loading rather than an empty queue", async () => {
    vi.spyOn(api, "unmappable").mockReturnValue(new Promise(() => {}));
    const { container } = show();
    await waitFor(() =>
      expect(container.querySelector('[aria-busy="true"]')).toBeInTheDocument(),
    );
    expect(screen.queryByText(/nothing is unmapped/i)).not.toBeInTheDocument();
  });

  it("never turns an API failure into an empty queue", async () => {
    vi.spyOn(api, "unmappable").mockRejectedValue(new ApiError(0, "service unreachable"));
    show();
    expect(await screen.findByRole("alert")).toHaveTextContent(/service unreachable/i);
    expect(screen.queryByText(/nothing is unmapped/i)).not.toBeInTheDocument();
  });

  it("explains an empty unmapped queue", async () => {
    vi.spyOn(api, "unmappable").mockResolvedValue(queue([]));
    show();
    expect(await screen.findByText("Nothing is unmapped")).toBeInTheDocument();
  });

  it("keeps the raw payload collapsed until asked for", async () => {
    vi.spyOn(api, "unmappable").mockResolvedValue(
      queue([row({ payload: { anchor_guess: "ord_9999" } })]),
    );
    const user = userEvent.setup();
    show();
    await screen.findByText("subscription.pending");
    expect(screen.queryByText(/anchor_guess/)).not.toBeInTheDocument();

    await user.click(screen.getByRole("button", { name: /raw payload/i }));
    expect(await screen.findByText(/anchor_guess/)).toBeInTheDocument();
    expect(screen.getByText(/ord_9999/)).toBeInTheDocument();
  });

  it("lists more than one unmapped webhook without key collisions", async () => {
    vi.spyOn(api, "unmappable").mockResolvedValue(
      queue([
        row({ webhook_event_id: 1, provider_event_id: "evt_a", event_type: "subscription.pending" }),
        row({ webhook_event_id: 2, provider_event_id: "evt_b", event_type: "invoice.unknown" }),
      ]),
    );
    show();
    expect(await screen.findByText("evt_a")).toBeInTheDocument();
    expect(screen.getByText("evt_b")).toBeInTheDocument();
    expect(screen.getByText("subscription.pending")).toBeInTheDocument();
    expect(screen.getByText("invoice.unknown")).toBeInTheDocument();
  });
});
