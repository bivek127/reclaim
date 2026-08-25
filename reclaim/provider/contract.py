"""Provider-agnostic result vocabulary for the payment boundary.

Nothing Razorpay-specific crosses this module: no SDK types, no raw provider
models, no provider status strings, no provider exceptions. The domain and the
future executor/reconciler see only what is defined here.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, NoReturn, Protocol

# Razorpay caps reference_id at 40 characters (documented). Our key is
# "rcv_" + base32(uuid4())[:26] = 30 characters, so it fits with room to spare.
MAX_REFERENCE_LENGTH = 40


class ProviderOutcome(str, Enum):
    """Normalized outcome of a money-moving provider request."""

    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    DUPLICATE_REFERENCE = "DUPLICATE_REFERENCE"
    TIMEOUT = "TIMEOUT"
    TRANSPORT_ERROR = "TRANSPORT_ERROR"
    PROVIDER_ERROR = "PROVIDER_ERROR"
    RATE_LIMITED = "RATE_LIMITED"
    UNPARSEABLE = "UNPARSEABLE"
    AUTH_ERROR = "AUTH_ERROR"
    UNKNOWN = "UNKNOWN"


# Outcomes that are not evidence either way; callers route these to AMBIGUOUS.
# RATE_LIMITED and PROVIDER_ERROR belong here because we cannot prove the
# provider processed nothing, and the safe direction is always toward ambiguity.
UNKNOWN_OUTCOMES = frozenset(
    {
        ProviderOutcome.TIMEOUT,
        ProviderOutcome.PROVIDER_ERROR,
        ProviderOutcome.RATE_LIMITED,
        ProviderOutcome.UNPARSEABLE,
        ProviderOutcome.UNKNOWN,
    }
)

# TRANSPORT_ERROR is deliberately absent above: zero bytes written means the
# request never reached the provider, so it is a resolved (negative) outcome,
# not an unknown one.
RESOLVED_OUTCOMES = frozenset(
    {
        ProviderOutcome.ACCEPTED,
        ProviderOutcome.REJECTED,
        ProviderOutcome.DUPLICATE_REFERENCE,
    }
)


class FetchOutcome(str, Enum):
    """Outcome of the read-only fetch.

    NO_EVIDENCE is a distinct value from NOT_FOUND on purpose. Treating a failed
    query as "not found" would license a second dispatch after a transient
    network problem, and a read-only fetch must never trigger one.
    """

    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    NO_EVIDENCE = "NO_EVIDENCE"


class LinkStatus(str, Enum):
    """Normalized payment-mechanism status.

    EXPIRED carries no "terminal failure" flag. Whether an expired mechanism is
    financially dead is unverified (ADR-006), so this boundary offers nothing a
    caller could mistake for that evidence.
    """

    CREATED = "CREATED"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    PAID = "PAID"
    EXPIRED = "EXPIRED"
    CANCELLED = "CANCELLED"
    UNKNOWN = "UNKNOWN"


class ErrorClass(str, Enum):
    """Deterministic classification of why a request did not succeed."""

    TIMEOUT = "TIMEOUT"
    NETWORK = "NETWORK"
    TRANSIENT_PROVIDER = "TRANSIENT_PROVIDER"
    RATE_LIMIT = "RATE_LIMIT"
    VALIDATION = "VALIDATION"
    AUTHENTICATION = "AUTHENTICATION"
    BUSINESS_REJECTION = "BUSINESS_REJECTION"
    DUPLICATE_REFERENCE = "DUPLICATE_REFERENCE"
    MALFORMED_RESPONSE = "MALFORMED_RESPONSE"
    UNKNOWN = "UNKNOWN"


class ProviderRequestInvalid(Exception):
    """The caller handed the adapter something it must never send. Not an outcome."""


class RetryChargeUnsupported(Exception):
    """No provider operation implements a direct retry-charge with a binding key."""


@dataclass(frozen=True)
class Customer:
    """Built by the caller from `financial_obligations.customer_ref`.

    The adapter never derives a customer from anything else, and never invents
    a destination.
    """

    name: str | None = None
    email: str | None = None
    contact: str | None = None


@dataclass(frozen=True)
class RequestRecord:
    """What we sent, for the audit trail. Never contains credentials."""

    operation: str
    method: str
    url: str
    body: dict[str, Any] | None


@dataclass(frozen=True)
class FetchResult:
    outcome: FetchOutcome
    provider_reference: str
    request: RequestRecord
    error_class: ErrorClass | None = None
    http_status: int | None = None
    provider_correlation_id: str | None = None
    link_status: LinkStatus | None = None
    provider_status_raw: str | None = None
    amount_minor: int | None = None
    amount_paid_minor: int | None = None
    currency: str | None = None
    expire_by: int | None = None
    short_url: str | None = None
    response_body: Any | None = None


@dataclass(frozen=True)
class CreateLinkResult:
    outcome: ProviderOutcome
    provider_reference: str
    request: RequestRecord
    error_class: ErrorClass | None = None
    http_status: int | None = None
    provider_correlation_id: str | None = None
    link_status: LinkStatus | None = None
    provider_status_raw: str | None = None
    short_url: str | None = None
    expire_by: int | None = None
    error_code: str | None = None
    error_description: str | None = None
    response_body: Any | None = None
    corroboration: FetchResult | None = None

    @property
    def is_unknown(self) -> bool:
        return self.outcome in UNKNOWN_OUTCOMES


class PaymentProvider(Protocol):
    def create_payment_link(
        self,
        *,
        reference_id: str,
        amount_minor: int,
        currency: str,
        customer: Customer,
        expire_by: int | None = None,
        description: str | None = None,
    ) -> CreateLinkResult: ...

    def fetch_by_reference(self, *, reference_id: str) -> FetchResult: ...

    def retry_charge(self, **kwargs: Any) -> NoReturn: ...

    def verify_webhook_signature(self, raw_body: bytes, signature: str) -> bool: ...
