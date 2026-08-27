import { describe, expect, it, vi, beforeEach, afterEach } from "vitest";
import { render, screen, within } from "@testing-library/react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { MemoryRouter } from "react-router-dom";
import { SystemPage } from "../SystemPage";
import { api, ApiError } from "@/lib/api";
import type { ActivityEvent, Health, Meta, Overview, SystemStatus } from "@/lib/types";

const STATES = [
  "NEW", "ENRICHING", "DIAGNOSING", "POLICY_EVAL", "ACTION_READY", "EXECUTING",
  "AWAITING_CUSTOMER", "ATTEMPT_FAILED", "AMBIGUOUS", "RECONCILING", "HALTED",
  "ESCALATED", "VERIFIED_RECOVERED", "VERIFIED_FAILED", "EXPIRED_UNRESOLVED",
];

const HEALTH: Health = { status: "ok", environment: "reclaim_dev" };

const META: Meta = {
  environment: "reclaim_dev",
  case_states: STATES,
  attention_states: ["ESCALATED", "AMBIGUOUS", "HALTED"],
  in_flight_states: ["EXECUTING", "RECONCILING", "AWAITING_CUSTOMER"],
  reviewable_actions: ["CREATE_PAYMENT_LINK"],
};

function system(over: Partial<SystemStatus> = {}): SystemStatus {
  return {
    breaker: {
      state: "CLOSED", consecutive_failures: 0,
      opened_at: null, reset_after: null, trip_cause: null,
    },
    leases_held: 0, leases_expired: 0, open_actions: 10,
    unresolved_attempts: 2, stale_writes_rejected: 0,
    ...over,
  };
}

function event(over: Partial<ActivityEvent> = {}): ActivityEvent {
  return {
    id: 272, occurred_at: new Date(Date.now() - 60_000).toISOString(),
    event_type: "breaker_reset", case_id: null, reason_code: "seed_reset",
    prev_state: null, new_state: null, worker_id: null, reviewer_ref: null,
    ...over,
  };
}

function overview(activity: ActivityEvent[]): Overview {
  const counts: Record<string, number> = Object.fromEntries(STATES.map((s) => [s, 0]));
  return {
    state_counts: counts, attention_total: 5, in_flight_total: 3,
    recovered_count: 2, recovered_amount_minor: 554900, pending_reviews: 2,
    oldest_pending_review_at: null,
    breaker_state: "CLOSED", breaker_consecutive_failures: 0,
    recent_activity: activity,
  };
}

function show() {
  const client = new QueryClient({
    defaultOptions: { queries: { retry: false, gcTime: 0 } },
  });
  return render(
    <QueryClientProvider client={client}>
      <MemoryRouter>
        <SystemPage />
      </MemoryRouter>
    </QueryClientProvider>,
  );
}

const DEFAULT_ACTIVITY = [
  event(),
  event({ id: 271, event_type: "state_transition", case_id: 13, reason_code: "breaker_open",
          prev_state: "ACTION_READY", new_state: "HALTED", worker_id: "executor" }),
  event({ id: 270, event_type: "breaker_opened", case_id: null, reason_code: "seed_demonstration" }),
];

beforeEach(() => {
  vi.spyOn(api, "health").mockResolvedValue(HEALTH);
  vi.spyOn(api, "meta").mockResolvedValue(META);
  vi.spyOn(api, "system").mockResolvedValue(system());
  vi.spyOn(api, "overview").mockResolvedValue(overview(DEFAULT_ACTIVITY));
});

afterEach(() => vi.restoreAllMocks());

describe("SystemPage — attachment and liveness", () => {
  it("names the database the API reports rather than guessing an environment", async () => {
    show();
    const attachment = within(await findSection("Attachment"));
    expect(await attachment.findByText("reclaim_dev")).toBeInTheDocument();
    expect(attachment.getByText("Responding")).toBeInTheDocument();
    expect(attachment.getByText("ok")).toBeInTheDocument();
  });

  it("reports the API as unreachable when the liveness probe fails", async () => {
    vi.spyOn(api, "health").mockRejectedValue(
      new ApiError(0, "Could not reach the recovery service."),
    );
    show();
    const attachment = within(await findSection("Attachment"));
    expect(await attachment.findByText("Unreachable")).toBeInTheDocument();
    // Liveness is not claimed from another endpoint's success.
    expect(attachment.getByText("Not reported")).toBeInTheDocument();
  });

  it("falls back to the vocabulary endpoint for the database name", async () => {
    vi.spyOn(api, "health").mockRejectedValue(new ApiError(0, "unreachable"));
    show();
    const attachment = within(await findSection("Attachment"));
    expect(await attachment.findByText("reclaim_dev")).toBeInTheDocument();
  });
});

describe("SystemPage — circuit breaker", () => {
  it("states plainly that dispatch is permitted when closed", async () => {
    show();
    const breaker = await screen.findByRole("region", { name: "CLOSED" });
    expect(within(breaker).getByText(/Dispatch to the provider is permitted/)).toBeInTheDocument();
    // A calm breaker must not borrow the outage language.
    expect(within(breaker).queryByText(/Recovery has not stopped/)).not.toBeInTheDocument();
  });

  it("shows an open breaker with its cause and the failures that tripped it", async () => {
    const openedAt = new Date(Date.now() - 300_000).toISOString();
    vi.spyOn(api, "system").mockResolvedValue(
      system({
        breaker: {
          state: "OPEN", consecutive_failures: 3, opened_at: openedAt,
          reset_after: new Date(Date.now() + 120_000).toISOString(),
          trip_cause: { reason: "development seed" },
        },
      }),
    );
    show();
    const breaker = await screen.findByRole("region", { name: "OPEN" });
    expect(within(breaker).getByText(/Dispatch to the provider is stopped/)).toBeInTheDocument();
    expect(within(breaker).getByText("3")).toBeInTheDocument();
    expect(within(breaker).getByText(/development seed/)).toBeInTheDocument();
  });

  it("does not claim the application is down when the breaker is open", async () => {
    vi.spyOn(api, "system").mockResolvedValue(
      system({
        breaker: {
          state: "OPEN", consecutive_failures: 3,
          opened_at: new Date().toISOString(), reset_after: null, trip_cause: null,
        },
      }),
    );
    show();
    const breaker = await screen.findByRole("region", { name: "OPEN" });
    expect(
      within(breaker).getByText(/Recovery has not stopped/),
    ).toBeInTheDocument();
    expect(
      within(breaker).getByText(/verification and human review continue to run/),
    ).toBeInTheDocument();
  });

  it("frames the reset time as due rather than promised, since nothing closes the breaker", async () => {
    vi.spyOn(api, "system").mockResolvedValue(
      system({
        breaker: {
          state: "OPEN", consecutive_failures: 3,
          opened_at: new Date().toISOString(),
          reset_after: new Date(Date.now() + 120_000).toISOString(),
          trip_cause: null,
        },
      }),
    );
    show();
    const breaker = await screen.findByRole("region", { name: "OPEN" });
    expect(within(breaker).getByText(/Reset due after/)).toBeInTheDocument();
    expect(
      within(breaker).getByText(/does not close on its own; a monitor job has to close it/),
    ).toBeInTheDocument();
  });

  it("counts failures under a still-closed breaker without implying it tripped", async () => {
    vi.spyOn(api, "system").mockResolvedValue(
      system({
        breaker: {
          state: "CLOSED", consecutive_failures: 2,
          opened_at: null, reset_after: null, trip_cause: null,
        },
      }),
    );
    show();
    const breaker = await screen.findByRole("region", { name: "CLOSED" });
    expect(
      within(breaker).getByText(/2 consecutive execution failures have been counted/),
    ).toBeInTheDocument();
    expect(within(breaker).getByText(/still open for traffic/)).toBeInTheDocument();
  });
});

describe("SystemPage — fail-closed behaviour", () => {
  it("reports the breaker as unknown rather than closed when status cannot be read", async () => {
    vi.spyOn(api, "system").mockRejectedValue(new ApiError(0, "unreachable"));
    show();
    const breaker = await screen.findByRole("region", { name: "Unknown" });
    expect(
      within(breaker).getByText(/not the same as a closed breaker/),
    ).toBeInTheDocument();
    expect(within(breaker).queryByText("CLOSED")).not.toBeInTheDocument();
  });

  it("shows no safety counters at all when the status query fails", async () => {
    vi.spyOn(api, "system").mockRejectedValue(new ApiError(0, "unreachable"));
    show();
    expect(await screen.findByText(/An unreadable counter is not a zero/)).toBeInTheDocument();
    expect(
      screen.queryByRole("list", { name: "Concurrency and safety counters" }),
    ).not.toBeInTheDocument();
  });

  it("keeps the rest of the page usable when one panel fails", async () => {
    vi.spyOn(api, "overview").mockRejectedValue(new ApiError(500, "boom"));
    show();
    expect(await screen.findByText("System events are unavailable")).toBeInTheDocument();
    // The breaker and attachment panels are unaffected.
    expect(await screen.findByRole("region", { name: "CLOSED" })).toBeInTheDocument();
    const attachment = within(await findSection("Attachment"));
    expect(await attachment.findByText("reclaim_dev")).toBeInTheDocument();
  });
});

describe("SystemPage — system-wide events", () => {
  it("lists only events that belong to no case", async () => {
    show();
    const events = await screen.findByRole("list", { name: "System-wide events" });
    const rows = within(events).getAllByRole("listitem");
    expect(rows).toHaveLength(2);
    expect(within(events).getByText("Circuit breaker reset")).toBeInTheDocument();
    expect(within(events).getByText("Circuit breaker opened")).toBeInTheDocument();
    // The case-scoped transition in the same feed is not a system event.
    expect(within(events).queryByText("State changed")).not.toBeInTheDocument();
  });

  it("marks each event as having no case, distinguishing it from case activity", async () => {
    show();
    const events = await screen.findByRole("list", { name: "System-wide events" });
    expect(within(events).getAllByText("No case — system-wide")).toHaveLength(2);
  });

  it("states the window it read from so an empty list is not read as none ever", async () => {
    vi.spyOn(api, "overview").mockResolvedValue(
      overview([event({ id: 1, event_type: "state_transition", case_id: 4 })]),
    );
    show();
    expect(
      await screen.findByText(/No system-wide events in this window/),
    ).toBeInTheDocument();
    expect(
      screen.getByText(/This is not a complete breaker history/),
    ).toBeInTheDocument();
  });
});

describe("SystemPage — safety signals", () => {
  it("frames a rejected stale write as the protection working", async () => {
    vi.spyOn(api, "system").mockResolvedValue(system({ stale_writes_rejected: 4 }));
    show();
    const counters = await screen.findByRole("list", { name: "Concurrency and safety counters" });
    expect(
      within(counters).getByText(/The protection worked; the case was not corrupted/),
    ).toBeInTheDocument();
  });

  it("does not raise routine lease activity into a signal", async () => {
    vi.spyOn(api, "system").mockResolvedValue(system({ leases_held: 12 }));
    show();
    const counters = await screen.findByRole("list", { name: "Concurrency and safety counters" });
    const held = within(counters).getByText("Leases held").closest("li");
    expect(held).not.toHaveClass("is-notable");
  });

  it("raises expired leases, which mean a worker stopped without releasing", async () => {
    vi.spyOn(api, "system").mockResolvedValue(system({ leases_expired: 3 }));
    show();
    const counters = await screen.findByRole("list", { name: "Concurrency and safety counters" });
    const expired = within(counters).getByText("Leases expired").closest("li");
    expect(expired).toHaveClass("is-notable");
    expect(within(counters).getByText(/The sweeper reclaims these/)).toBeInTheDocument();
  });

  it("offers a route into the cases behind an unresolved-attempt count", async () => {
    show();
    const counters = await screen.findByRole("list", { name: "Concurrency and safety counters" });
    const link = within(counters).getByRole("link", { name: "Review unresolved cases" });
    expect(link).toHaveAttribute("href", "/cases?state=AMBIGUOUS&state=RECONCILING");
  });
});

describe("SystemPage — domain vocabulary", () => {
  it("groups the API's states using the API's own attention and in-flight sets", async () => {
    show();
    expect(await screen.findByText("AMBIGUOUS")).toBeInTheDocument();
    const needsHuman = (await screen.findByText(/Needs a human/)).closest("div");
    expect(within(needsHuman as HTMLElement).getByText("ESCALATED")).toBeInTheDocument();
    expect(within(needsHuman as HTMLElement).getByText("HALTED")).toBeInTheDocument();
  });

  it("treats VERIFIED_FAILED as a terminal outcome, not a failure of the system", async () => {
    show();
    const terminal = (await screen.findByText("Terminal")).closest("div");
    expect(within(terminal as HTMLElement).getByText("VERIFIED_FAILED")).toBeInTheDocument();
    expect(
      within(terminal as HTMLElement).getByText("Confirmed not recovered. Case closed."),
    ).toBeInTheDocument();
  });

  it("calls out a state the API reports that this console cannot describe", async () => {
    vi.spyOn(api, "meta").mockResolvedValue({
      ...META, case_states: [...STATES, "QUARANTINED"],
    });
    show();
    const drift = await screen.findByRole("status");
    expect(drift).toHaveTextContent(/running a newer domain than this build knows about/);
    // Named in the notice, not merely rendered as an unexplained row.
    expect(within(drift).getByText("QUARANTINED")).toBeInTheDocument();
  });
});

describe("SystemPage — navigation", () => {
  it("routes to the operational surfaces rather than duplicating them", async () => {
    show();
    const nav = await screen.findByRole("navigation", { name: "Operational surfaces" });
    expect(within(nav).getByRole("link", { name: "Cases needing a human" }))
      .toHaveAttribute("href", "/cases?needs_attention=true");
    expect(within(nav).getByRole("link", { name: "Pending reviews" }))
      .toHaveAttribute("href", "/reviews");
  });
});

/** Locates a Section by its heading and returns the section element. */
async function findSection(title: string): Promise<HTMLElement> {
  const heading = await screen.findByRole("heading", { name: title });
  return heading.closest("section") as HTMLElement;
}
