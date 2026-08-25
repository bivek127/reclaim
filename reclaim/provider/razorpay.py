"""Razorpay adapter: request construction, status normalization, error classification.

This module is the only place Razorpay's wire vocabulary appears. It performs no
database access, no state transitions, no lease or fencing handling, no policy,
and no reconciliation decisions — the domain layer owns all of that. It returns
normalized results rich enough for the executor to write its `provider_requests`
row without ever seeing a provider string.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any, NoReturn
from urllib.parse import urlencode

from reclaim.provider.config import ProviderConfig
from reclaim.provider.contract import (
    MAX_REFERENCE_LENGTH,
    CreateLinkResult,
    Customer,
    ErrorClass,
    FetchOutcome,
    FetchResult,
    LinkStatus,
    ProviderOutcome,
    ProviderRequestInvalid,
    RequestRecord,
    RetryChargeUnsupported,
)
from reclaim.provider.transport import (
    HttpClientTransport,
    HttpResponse,
    Transport,
    TransportFailure,
    TransportPhase,
)

PAYMENT_LINKS_PATH = "/v1/payment_links"

CREATE_PAYMENT_LINK = "create_payment_link"
FETCH_BY_REFERENCE = "fetch_by_reference"

# Documented Razorpay Payment Link statuses. Anything absent from this map
# normalizes to LinkStatus.UNKNOWN — never to the nearest-looking known value.
_LINK_STATUS = {
    "created": LinkStatus.CREATED,
    "partially_paid": LinkStatus.PARTIALLY_PAID,
    "paid": LinkStatus.PAID,
    "expired": LinkStatus.EXPIRED,
    "cancelled": LinkStatus.CANCELLED,
}


class RazorpayAdapter:
    def __init__(self, config: ProviderConfig, transport: Transport | None = None) -> None:
        self.config = config
        self._transport: Transport = transport or HttpClientTransport(config.base_url)

    # ---- operations ----------------------------------------------------

    def create_payment_link(
        self,
        *,
        reference_id: str,
        amount_minor: int,
        currency: str,
        customer: Customer,
        expire_by: int | None = None,
        description: str | None = None,
    ) -> CreateLinkResult:
        """POST /v1/payment_links. reference_id is the persisted idempotency key."""
        _validate_reference(reference_id)
        if amount_minor <= 0:
            raise ProviderRequestInvalid("amount_minor must be positive")
        if not currency or currency != currency.upper():
            raise ProviderRequestInvalid("currency must be an upper-case ISO code")
        if not (customer.email or customer.contact):
            raise ProviderRequestInvalid("customer needs an email or a contact")

        deadline = expire_by or int(time.time()) + self.config.payment_link_ttl_seconds
        body = _create_body(
            reference_id=reference_id,
            amount_minor=amount_minor,
            currency=currency,
            customer=customer,
            expire_by=deadline,
            description=description,
        )
        record = RequestRecord(
            operation=CREATE_PAYMENT_LINK,
            method="POST",
            url=PAYMENT_LINKS_PATH,
            body=body,
        )

        try:
            response = self._send("POST", PAYMENT_LINKS_PATH, body=body, timeout=self.config.create_link_timeout_seconds)
        except TransportFailure as failure:
            outcome, error_class = _classify_transport(failure)
            return CreateLinkResult(
                outcome=outcome,
                provider_reference=reference_id,
                request=record,
                error_class=error_class,
                expire_by=deadline,
            )

        return self._classify_create(
            response,
            reference_id=reference_id,
            record=record,
            expire_by=deadline,
        )

    def fetch_by_reference(self, *, reference_id: str) -> FetchResult:
        """GET /v1/payment_links?reference_id=... Read-only. Moves nothing."""
        _validate_reference(reference_id)
        path = f"{PAYMENT_LINKS_PATH}?{urlencode({'reference_id': reference_id})}"
        record = RequestRecord(
            operation=FETCH_BY_REFERENCE, method="GET", url=path, body=None
        )

        try:
            response = self._send("GET", path, body=None, timeout=self.config.fetch_timeout_seconds)
        except TransportFailure as failure:
            _, error_class = _classify_transport(failure)
            return FetchResult(
                outcome=FetchOutcome.NO_EVIDENCE,
                provider_reference=reference_id,
                request=record,
                error_class=error_class,
            )

        return _classify_fetch(response, reference_id=reference_id, record=record)

    def retry_charge(self, **kwargs: Any) -> NoReturn:
        """Razorpay exposes no retry-charge operation with a binding key (ADR-005)."""
        raise RetryChargeUnsupported(
            "RETRY_CHARGE is not implementable: Razorpay exposes no merchant-facing "
            "charge-retry endpoint accepting a caller-supplied idempotency key "
            "(§19.1a, ADR-005)"
        )

    def verify_webhook_signature(self, raw_body: bytes, signature: str) -> bool:
        """Verify an inbound webhook signature. Uses this adapter's configured
        `RAZORPAY_WEBHOOK_SECRET`.

        Returns False, never raises, if no webhook secret is configured — an
        unconfigured secret must fail closed, not be treated as "skip verification."
        """
        return verify_webhook_signature(raw_body, signature, self.config.webhook_secret or "")

    # ---- internals -----------------------------------------------------

    def _send(
        self, method: str, path: str, *, body: dict[str, Any] | None, timeout: int
    ) -> HttpResponse:
        headers = {
            "Authorization": f"Basic {self._basic_auth()}",
            "Accept": "application/json",
            "User-Agent": "reclaim/0.1",
        }
        payload: bytes | None = None
        if body is not None:
            payload = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        return self._transport.request(
            method,
            path,
            headers=headers,
            body=payload,
            connect_timeout=self.config.connect_timeout_seconds,
            read_timeout=timeout,
        )

    def _basic_auth(self) -> str:
        raw = f"{self.config.key_id}:{self.config.key_secret}".encode("utf-8")
        return base64.b64encode(raw).decode("ascii")

    def _classify_create(
        self,
        response: HttpResponse,
        *,
        reference_id: str,
        record: RequestRecord,
        expire_by: int,
    ) -> CreateLinkResult:
        status = response.status
        parsed = _parse_json(response.body)

        if 200 <= status < 300:
            return _accepted_or_unparseable(
                parsed,
                status=status,
                reference_id=reference_id,
                record=record,
                expire_by=expire_by,
            )

        error_code, error_description = _error_fields(parsed)
        common: dict[str, Any] = {
            "provider_reference": reference_id,
            "request": record,
            "http_status": status,
            "error_code": error_code,
            "error_description": error_description,
            "response_body": parsed,
            "expire_by": expire_by,
        }

        if status in (401, 403):
            return CreateLinkResult(
                outcome=ProviderOutcome.AUTH_ERROR,
                error_class=ErrorClass.AUTHENTICATION,
                **common,
            )
        if status == 429:
            return CreateLinkResult(
                outcome=ProviderOutcome.RATE_LIMITED,
                error_class=ErrorClass.RATE_LIMIT,
                **common,
            )
        if 500 <= status < 600:
            return CreateLinkResult(
                outcome=ProviderOutcome.PROVIDER_ERROR,
                error_class=ErrorClass.TRANSIENT_PROVIDER,
                **common,
            )
        if 400 <= status < 500:
            return self._corroborate(reference_id=reference_id, status=status, common=common)

        return CreateLinkResult(
            outcome=ProviderOutcome.UNKNOWN, error_class=ErrorClass.UNKNOWN, **common
        )

    def _corroborate(
        self, *, reference_id: str, status: int, common: dict[str, Any]
    ) -> CreateLinkResult:
        """Razorpay's duplicate error is a generic BAD_REQUEST_ERROR whose
        description text is not stable across sources, so the duplicate is resolved
        by read-only evidence rather than by matching a guessed string (ADR-007).
        """
        corroboration = self.fetch_by_reference(reference_id=reference_id)

        if corroboration.outcome is FetchOutcome.FOUND:
            return CreateLinkResult(
                outcome=ProviderOutcome.DUPLICATE_REFERENCE,
                error_class=ErrorClass.DUPLICATE_REFERENCE,
                provider_correlation_id=corroboration.provider_correlation_id,
                link_status=corroboration.link_status,
                provider_status_raw=corroboration.provider_status_raw,
                short_url=corroboration.short_url,
                corroboration=corroboration,
                **common,
            )

        if corroboration.outcome is FetchOutcome.NOT_FOUND:
            return CreateLinkResult(
                outcome=ProviderOutcome.REJECTED,
                error_class=(
                    ErrorClass.VALIDATION if status == 400 else ErrorClass.BUSINESS_REJECTION
                ),
                corroboration=corroboration,
                **common,
            )

        # The read proved nothing. We cannot tell a duplicate from a rejection,
        # so the outcome stays unknown and the case routes to AMBIGUOUS.
        return CreateLinkResult(
            outcome=ProviderOutcome.UNKNOWN,
            error_class=ErrorClass.UNKNOWN,
            corroboration=corroboration,
            **common,
        )


# ---- module-level helpers ----------------------------------------------


def _validate_reference(reference_id: str) -> None:
    if not reference_id:
        raise ProviderRequestInvalid("reference_id is required")
    if len(reference_id) > MAX_REFERENCE_LENGTH:
        raise ProviderRequestInvalid(
            f"reference_id exceeds the provider's {MAX_REFERENCE_LENGTH}-character limit"
        )


def _create_body(
    *,
    reference_id: str,
    amount_minor: int,
    currency: str,
    customer: Customer,
    expire_by: int,
    description: str | None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "amount": amount_minor,
        "currency": currency,
        "reference_id": reference_id,
        "expire_by": expire_by,
    }
    contact: dict[str, str] = {}
    if customer.name:
        contact["name"] = customer.name
    if customer.email:
        contact["email"] = customer.email
    if customer.contact:
        contact["contact"] = customer.contact
    payload["customer"] = contact
    if description:
        payload["description"] = description
    return payload


def _parse_json(body: bytes) -> Any | None:
    try:
        return json.loads(body.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError, ValueError):
        return None


def _error_fields(parsed: Any | None) -> tuple[str | None, str | None]:
    if not isinstance(parsed, dict):
        return None, None
    error = parsed.get("error")
    if not isinstance(error, dict):
        return None, None
    code = error.get("code")
    description = error.get("description")
    return (
        code if isinstance(code, str) else None,
        description if isinstance(description, str) else None,
    )


def normalize_link_status(raw: Any) -> LinkStatus:
    if not isinstance(raw, str):
        return LinkStatus.UNKNOWN
    return _LINK_STATUS.get(raw.lower(), LinkStatus.UNKNOWN)


def _accepted_or_unparseable(
    parsed: Any | None,
    *,
    status: int,
    reference_id: str,
    record: RequestRecord,
    expire_by: int,
) -> CreateLinkResult:
    """A 2xx without a usable correlation id is not a success — it is unknown."""
    link_id = parsed.get("id") if isinstance(parsed, dict) else None
    if not isinstance(link_id, str) or not link_id:
        return CreateLinkResult(
            outcome=ProviderOutcome.UNPARSEABLE,
            provider_reference=reference_id,
            request=record,
            error_class=ErrorClass.MALFORMED_RESPONSE,
            http_status=status,
            response_body=parsed,
            expire_by=expire_by,
        )

    assert isinstance(parsed, dict)
    raw_status = parsed.get("status")
    returned = parsed.get("expire_by")
    short_url = parsed.get("short_url")
    return CreateLinkResult(
        outcome=ProviderOutcome.ACCEPTED,
        provider_reference=reference_id,
        request=record,
        http_status=status,
        provider_correlation_id=link_id,
        link_status=normalize_link_status(raw_status),
        provider_status_raw=raw_status if isinstance(raw_status, str) else None,
        short_url=short_url if isinstance(short_url, str) else None,
        expire_by=returned if isinstance(returned, int) else expire_by,
        response_body=parsed,
    )


def _classify_transport(failure: TransportFailure) -> tuple[ProviderOutcome, ErrorClass]:
    """CONNECT is the only phase that proves zero bytes were written."""
    if failure.phase is TransportPhase.CONNECT:
        return ProviderOutcome.TRANSPORT_ERROR, (
            ErrorClass.TIMEOUT if failure.timed_out else ErrorClass.NETWORK
        )
    if failure.timed_out:
        return ProviderOutcome.TIMEOUT, ErrorClass.TIMEOUT
    return ProviderOutcome.UNKNOWN, ErrorClass.NETWORK


def _classify_fetch(
    response: HttpResponse, *, reference_id: str, record: RequestRecord
) -> FetchResult:
    status = response.status
    parsed = _parse_json(response.body)

    if not 200 <= status < 300:
        if status in (401, 403):
            error_class = ErrorClass.AUTHENTICATION
        elif status == 429:
            error_class = ErrorClass.RATE_LIMIT
        elif 500 <= status < 600:
            error_class = ErrorClass.TRANSIENT_PROVIDER
        elif 400 <= status < 500:
            error_class = ErrorClass.VALIDATION
        else:
            error_class = ErrorClass.UNKNOWN
        # A failed query is never evidence of absence.
        return FetchResult(
            outcome=FetchOutcome.NO_EVIDENCE,
            provider_reference=reference_id,
            request=record,
            error_class=error_class,
            http_status=status,
            response_body=parsed,
        )

    links = parsed.get("payment_links") if isinstance(parsed, dict) else None
    if not isinstance(links, list):
        return FetchResult(
            outcome=FetchOutcome.NO_EVIDENCE,
            provider_reference=reference_id,
            request=record,
            error_class=ErrorClass.MALFORMED_RESPONSE,
            http_status=status,
            response_body=parsed,
        )

    if not links:
        return FetchResult(
            outcome=FetchOutcome.NOT_FOUND,
            provider_reference=reference_id,
            request=record,
            http_status=status,
            response_body=parsed,
        )

    if len(links) > 1:
        # reference_id is supposed to be unique. Several matches is an integrity
        # anomaly, not a result — never pick one.
        return FetchResult(
            outcome=FetchOutcome.NO_EVIDENCE,
            provider_reference=reference_id,
            request=record,
            error_class=ErrorClass.MALFORMED_RESPONSE,
            http_status=status,
            response_body=parsed,
        )

    link = links[0]
    if not isinstance(link, dict) or not isinstance(link.get("id"), str):
        return FetchResult(
            outcome=FetchOutcome.NO_EVIDENCE,
            provider_reference=reference_id,
            request=record,
            error_class=ErrorClass.MALFORMED_RESPONSE,
            http_status=status,
            response_body=parsed,
        )

    raw_status = link.get("status")
    return FetchResult(
        outcome=FetchOutcome.FOUND,
        provider_reference=reference_id,
        request=record,
        http_status=status,
        provider_correlation_id=link["id"],
        link_status=normalize_link_status(raw_status),
        provider_status_raw=raw_status if isinstance(raw_status, str) else None,
        amount_minor=_int_or_none(link.get("amount")),
        amount_paid_minor=_int_or_none(link.get("amount_paid")),
        currency=link.get("currency") if isinstance(link.get("currency"), str) else None,
        expire_by=_int_or_none(link.get("expire_by")),
        short_url=link.get("short_url") if isinstance(link.get("short_url"), str) else None,
        response_body=parsed,
    )


def _int_or_none(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def verify_webhook_signature(raw_body: bytes, signature: str, secret: str) -> bool:
    """HMAC-SHA256 over the raw body with the webhook secret.

    Feeds `ingest_webhook(signature_valid=...)`. Comparison is constant-time.
    """
    if not signature or not secret:
        return False
    expected = hmac.new(secret.encode("utf-8"), raw_body, hashlib.sha256).hexdigest()
    # compare_digest rejects non-ASCII str arguments outright (TypeError). Compare as
    # bytes instead, so a malformed header is rejected rather than crashing the verifier.
    return hmac.compare_digest(expected.encode("utf-8"), signature.encode("utf-8"))
