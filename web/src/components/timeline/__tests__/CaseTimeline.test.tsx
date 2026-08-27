import { describe, expect, it } from "vitest";
import { render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MemoryRouter } from "react-router-dom";
import { CaseTimeline } from "../CaseTimeline";
import type { AuditEvent, CaseHistory } from "@/lib/types";

function ev(over: Partial<AuditEvent> & { id: number }): AuditEvent {
  return {
    occurred_at: "2026-08-27T16:28:22.800+05:30", event_type: "state_transition",
    obligation_id: 1, case_id: 1, action_id: null, attempt_id: null,
    provider_request_id: null, worker_id: null, fencing_token: null,
    prev_state: null, new_state: null, reason_code: null, model: null,
    model_version: null, policy_version: null, reviewer_ref: null,
    provider_correlation_id: null, detail: {}, ...over,
  };
}

function hist(timeline: AuditEvent[], over: Partial<CaseHistory> = {}): CaseHistory {
  return {
    case_id: 1, obligation_id: 1, created: true, deduplicated: false,
    timeline, state_changes: [], provider_correlation_ids: [],
    provider_references: [], workers: [], fencing_tokens: [],
    stale_writes: [], unreconstructable: [], ...over,
  };
}

function show(history: CaseHistory, path = "/cases/1/timeline") {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <CaseTimeline history={history} />
    </MemoryRouter>,
  );
}

describe("case audit timeline", () => {
  it("renders events in the order the API returned them", () => {
    show(hist([
      ev({ id: 1, event_type: "case_created", new_state: "NEW" }),
      ev({ id: 2, event_type: "policy_decision", detail: { verdict: "ALLOW", selected_action: "CREATE_PAYMENT_LINK" } }),
      ev({ id: 3, event_type: "verification_recorded", detail: { agrees: true, webhook_status: "SUCCESS", query_status: "PAID" } }),
    ]));
    const titles = screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent);
    expect(titles).toEqual(["Case opened", "Policy decided", "Independently verified"]);
  });

  it("keeps materially different event classes distinct", () => {
    show(hist([
      ev({ id: 1, event_type: "policy_decision", detail: { verdict: "ALLOW" } }),
      ev({ id: 2, event_type: "diagnosis_produced", detail: { cause: "UNKNOWN", source: "LLM" } }),
      ev({ id: 3, event_type: "provider_response_received", detail: { provider_outcome: "ACCEPTED" } }),
    ]));
    // Scoped to the record itself: the same words also label the filter chips.
    const list = within(screen.getByRole("list", { name: "Recorded events" }));
    expect(list.getByText("Policy")).toBeInTheDocument();
    expect(list.getByText("Diagnosis")).toBeInTheDocument();
    expect(list.getByText("Provider")).toBeInTheDocument();
  });

  it("shows state transitions as an explicit from/to pair", () => {
    show(hist([
      ev({ id: 1, prev_state: "POLICY_EVAL", new_state: "ACTION_READY", reason_code: "policy_allow_create_link" }),
    ]));
    expect(screen.getByText("POLICY_EVAL")).toBeInTheDocument();
    expect(screen.getByText("ACTION_READY")).toBeInTheDocument();
    expect(screen.getByText("policy_allow_create_link")).toBeInTheDocument();
  });

  it("marks events written in the same instant so ordering is unambiguous", () => {
    show(hist([
      ev({ id: 1, event_type: "policy_decision", occurred_at: "2026-08-27T16:28:22.827+05:30", detail: { verdict: "ALLOW" } }),
      ev({ id: 2, occurred_at: "2026-08-27T16:28:22.827+05:30", prev_state: "POLICY_EVAL", new_state: "ACTION_READY" }),
    ]));
    expect(screen.getByText(/same instant · #2/)).toBeInTheDocument();
  });

  it("shows clock time to millisecond precision", () => {
    show(hist([ev({ id: 1, occurred_at: "2026-08-27T16:28:22.833+05:30" })]));
    expect(screen.getByText(/16:28:22\.833/)).toBeInTheDocument();
  });

  it("keeps a provider claim distinct from independent verification", () => {
    show(hist([
      ev({ id: 1, event_type: "provider_response_received", detail: { provider_outcome: "ACCEPTED", target_state: "AWAITING_CUSTOMER" } }),
      ev({ id: 2, event_type: "verification_recorded", detail: { agrees: true, webhook_status: "SUCCESS", query_status: "PAID" } }),
    ]));
    expect(screen.getByText(/Provider reported ACCEPTED/)).toBeInTheDocument();
    expect(screen.getByText(/an independent query returned PAID; both agree/)).toBeInTheDocument();
    // A provider answer is never titled as a verification.
    expect(screen.getByRole("heading", { name: /Provider answered/ })).toBeInTheDocument();
  });

  it("does not present a disagreeing verification as recovery", () => {
    show(hist([
      ev({ id: 1, event_type: "verification_recorded", detail: { agrees: false, webhook_status: "SUCCESS", query_status: "EXPIRED" } }),
    ]));
    expect(screen.getByRole("heading", { name: "Verification disagreed" })).toBeInTheDocument();
    expect(screen.getByText(/No revenue recognised/)).toBeInTheDocument();
  });

  it("presents diagnosis as advisory", () => {
    show(hist([
      ev({ id: 1, event_type: "diagnosis_produced", detail: { cause: "INSUFFICIENT_FUNDS", source: "LLM" } }),
    ]));
    expect(screen.getByText(/Advisory only/)).toBeInTheDocument();
  });

  it("explains a rejected stale write as a safety event", () => {
    show(hist([ev({ id: 1, event_type: "stale_write_rejected", worker_id: "slow", fencing_token: 3 })]));
    expect(screen.getByRole("heading", { name: "Stale write rejected" })).toBeInTheDocument();
    expect(screen.getByText(/The case was protected/)).toBeInTheDocument();
  });

  it("hides routine lease events by default but reports how many", () => {
    show(hist([
      ev({ id: 1, event_type: "case_created" }),
      ev({ id: 2, event_type: "lease_claimed", worker_id: "policy", fencing_token: 1 }),
      ev({ id: 3, event_type: "lease_claimed", worker_id: "executor", fencing_token: 2 }),
    ]));
    expect(screen.getByRole("status")).toHaveTextContent("Showing 1 of 3");
    expect(screen.getByText(/2 routine lease events hidden/)).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: "Lease claimed" })).not.toBeInTheDocument();
  });

  it("reveals routine events on request without changing their order", async () => {
    const user = userEvent.setup();
    show(hist([
      ev({ id: 1, event_type: "case_created" }),
      ev({ id: 2, event_type: "lease_claimed", worker_id: "policy", fencing_token: 1 }),
      ev({ id: 3, event_type: "policy_decision", detail: { verdict: "ALLOW" } }),
    ]));
    await user.click(screen.getByRole("button", { name: /routine concurrency/i }));
    const titles = screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent);
    expect(titles).toEqual(["Case opened", "Lease claimed", "Policy decided"]);
  });

  it("filters by event class without reordering or inventing rows", async () => {
    const user = userEvent.setup();
    show(hist([
      ev({ id: 1, event_type: "case_created" }),
      ev({ id: 2, event_type: "policy_decision", detail: { verdict: "ALLOW" } }),
      ev({ id: 3, prev_state: "A", new_state: "B" }),
    ]));
    await user.click(screen.getByRole("button", { name: /^Policy/ }));
    const titles = screen.getAllByRole("heading", { level: 3 }).map((h) => h.textContent);
    expect(titles).toEqual(["Policy decided"]);
    expect(screen.getByRole("status")).toHaveTextContent("Showing 1 of 3");
  });

  it("says the record is filtered, not empty, when nothing matches", async () => {
    const user = userEvent.setup();
    show(hist([ev({ id: 1, event_type: "case_created" })]), "/cases/1/timeline?cat=review");
    expect(await screen.findByText(/No events match this filter/)).toBeInTheDocument();
    expect(screen.getByText(/still in the case history/)).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: /show everything/i }));
    expect(screen.getByRole("heading", { name: "Case opened" })).toBeInTheDocument();
  });

  it("numbers events by their place in the full record, not the filtered view", async () => {
    const user = userEvent.setup();
    show(hist([
      ev({ id: 1, event_type: "case_created", occurred_at: "2026-08-27T16:28:22.100+05:30" }),
      ev({ id: 2, event_type: "lease_claimed", occurred_at: "2026-08-27T16:28:22.100+05:30" }),
      ev({ id: 3, event_type: "policy_decision", occurred_at: "2026-08-27T16:28:22.100+05:30", detail: { verdict: "ALLOW" } }),
    ]));
    await user.click(screen.getByRole("button", { name: /^Policy/ }));
    const toggle = screen.getByRole("button", { name: /technical details/i });
    await user.click(toggle);
    // Third in the authoritative record even though it is the only row shown.
    expect(screen.getByText("#3")).toBeInTheDocument();
  });

  it("keeps technical identifiers behind a disclosure", async () => {
    const user = userEvent.setup();
    show(hist([
      ev({
        id: 7, event_type: "provider_response_received", action_id: 3, attempt_id: 4,
        provider_correlation_id: "plink_ABC", worker_id: "executor", fencing_token: 5,
        detail: { provider_outcome: "ACCEPTED", provider_reference: "rcv_XYZ" },
      }),
    ]));
    expect(screen.queryByText("Correlation id")).not.toBeInTheDocument();
    const toggle = screen.getByRole("button", { name: /technical details/i });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    await user.click(toggle);
    expect(toggle).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText("Correlation id")).toBeInTheDocument();
    expect(screen.getByText("Attempt id")).toBeInTheDocument();
    expect(screen.getByText("Recorded evidence")).toBeInTheDocument();
  });

  it("renders an event with no detail payload and no identifiers", async () => {
    const user = userEvent.setup();
    show(hist([ev({ id: 1, event_type: "lease_released" })]), "/cases/1/timeline?routine=1");
    await user.click(screen.getByRole("button", { name: /technical details/i }));
    expect(screen.getByText("Event id")).toBeInTheDocument();
    expect(screen.queryByText("Recorded evidence")).not.toBeInTheDocument();
  });

  it("shows an unrecognised event type as itself", () => {
    show(hist([ev({ id: 1, event_type: "some_future_event" })]));
    expect(screen.getByRole("heading", { name: "some_future_event" })).toBeInTheDocument();
    const list = within(screen.getByRole("list", { name: "Recorded events" }));
    expect(list.getByText("Other")).toBeInTheDocument();
  });

  it("formats monetary detail through the integer-safe path", async () => {
    const user = userEvent.setup();
    show(hist([
      ev({ id: 1, event_type: "verification_recorded",
           detail: { agrees: true, verified_amount_minor: 425000, currency: "INR" } }),
    ]));
    await user.click(screen.getByRole("button", { name: /technical details/i }));
    expect(within(document.body).getByText("₹4,250.00")).toBeInTheDocument();
  });
});
