"""Provider boundary. Nothing Razorpay-specific crosses this package."""

from reclaim.provider.config import (
    ProviderConfig,
    ProviderConfigError,
    load_provider_config,
)
from reclaim.provider.contract import (
    RESOLVED_OUTCOMES,
    UNKNOWN_OUTCOMES,
    CreateLinkResult,
    Customer,
    ErrorClass,
    FetchOutcome,
    FetchResult,
    LinkStatus,
    PaymentProvider,
    ProviderOutcome,
    ProviderRequestInvalid,
    RequestRecord,
    RetryChargeUnsupported,
)
from reclaim.provider.razorpay import (
    RazorpayAdapter,
    normalize_link_status,
    verify_webhook_signature,
)
from reclaim.provider.transport import (
    HttpClientTransport,
    HttpResponse,
    Transport,
    TransportFailure,
    TransportPhase,
)

__all__ = [
    "ProviderConfig",
    "ProviderConfigError",
    "load_provider_config",
    "CreateLinkResult",
    "Customer",
    "ErrorClass",
    "FetchOutcome",
    "FetchResult",
    "LinkStatus",
    "PaymentProvider",
    "ProviderOutcome",
    "ProviderRequestInvalid",
    "RequestRecord",
    "RetryChargeUnsupported",
    "RESOLVED_OUTCOMES",
    "UNKNOWN_OUTCOMES",
    "RazorpayAdapter",
    "normalize_link_status",
    "verify_webhook_signature",
    "HttpClientTransport",
    "HttpResponse",
    "Transport",
    "TransportFailure",
    "TransportPhase",
]
