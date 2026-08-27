import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import { StatusBadge } from "../StatusBadge";
import { Money } from "../Money";
import { Identifier } from "../Identifier";
import { EmptyState, ErrorState } from "../States";
import { ApiError } from "@/lib/api";

describe("StatusBadge", () => {
  it("always shows the state as text, never colour alone", () => {
    render(<StatusBadge state="AWAITING_CUSTOMER" />);
    expect(screen.getByText("Awaiting Customer")).toBeInTheDocument();
  });

  it("can show the literal domain state name", () => {
    render(<StatusBadge state="VERIFIED_RECOVERED" variant="code" />);
    expect(screen.getByText("VERIFIED_RECOVERED")).toBeInTheDocument();
  });

  it("explains what the state means for the operator", () => {
    render(<StatusBadge state="AMBIGUOUS" />);
    expect(screen.getByTitle(/money may or may not have moved/i)).toBeInTheDocument();
  });

  it("renders an unknown state without crashing", () => {
    render(<StatusBadge state="FUTURE_STATE" />);
    expect(screen.getByText("Future State")).toBeInTheDocument();
  });
});

describe("Money", () => {
  it("renders the formatted amount and an accessible description", () => {
    render(<Money minorUnits={425000} currency="INR" />);
    expect(screen.getByText("₹4,250.00")).toBeInTheDocument();
    expect(screen.getByText("4250.00 INR")).toBeInTheDocument();
  });

  it("can show the currency code alongside the amount", () => {
    render(<Money minorUnits={100} currency="USD" showCode />);
    expect(screen.getByText("USD")).toBeInTheDocument();
  });
});

describe("Identifier", () => {
  it("renders a dash when there is no identifier", () => {
    render(<Identifier value={null} />);
    expect(screen.getByText("—")).toBeInTheDocument();
  });

  it("truncates long references but keeps the full value available", () => {
    const long = "rcv_SWSHNA3MPFBCBPCMSGLPHOEL2Q";
    render(<Identifier value={long} truncate label="Provider reference" />);
    expect(screen.getByTitle(new RegExp(long))).toBeInTheDocument();
    expect(screen.getByText(/^rcv_SWSHNA3MPFBCBP…$/)).toBeInTheDocument();
  });
});

describe("error and empty states", () => {
  it("shows the server's own explanation rather than a generic message", () => {
    render(<ErrorState error={new ApiError(409, "This case is not awaiting review.")} />);
    expect(screen.getByText("This case is not awaiting review.")).toBeInTheDocument();
  });

  it("warns when content on screen may be stale", () => {
    render(<ErrorState error={new Error("boom")} stale />);
    expect(screen.getByText(/may no longer be current/i)).toBeInTheDocument();
  });

  it("uses an alert role so failures are announced", () => {
    render(<ErrorState error={new Error("boom")} />);
    expect(screen.getByRole("alert")).toBeInTheDocument();
  });

  it("empty states explain what to do next", () => {
    render(<EmptyState title="No cases" description="Try widening the filters." />);
    expect(screen.getByText("No cases")).toBeInTheDocument();
    expect(screen.getByText("Try widening the filters.")).toBeInTheDocument();
  });
});
