"""LLM package — Ollama client, schema, prompt, fallback."""

from reclaim.llm.client import (
    LlmClient,
    LlmEmptyResponse,
    LlmError,
    LlmResponse,
    LlmTimeout,
    LlmUnreachable,
    OllamaClient,
    OllamaConfig,
    ScriptedLlm,
    UnreachableLlm,
)
from reclaim.llm.fallback import FAILURE_CODE_TO_CAUSE, fallback_cause, fallback_payload
from reclaim.llm.prompt import (
    PROMPT_VERSION,
    TrustedDiagnosisContext,
    UntrustedDiagnosisContext,
    build_prompt,
)
from reclaim.llm.schema import (
    ACTIONS,
    CAUSES,
    DiagnosisPayload,
    InvalidEnum,
    SchemaViolation,
    validate_payload,
)

__all__ = [
    "ACTIONS",
    "CAUSES",
    "FAILURE_CODE_TO_CAUSE",
    "PROMPT_VERSION",
    "DiagnosisPayload",
    "InvalidEnum",
    "LlmClient",
    "LlmEmptyResponse",
    "LlmError",
    "LlmResponse",
    "LlmTimeout",
    "LlmUnreachable",
    "OllamaClient",
    "OllamaConfig",
    "SchemaViolation",
    "ScriptedLlm",
    "TrustedDiagnosisContext",
    "UnreachableLlm",
    "UntrustedDiagnosisContext",
    "build_prompt",
    "fallback_cause",
    "fallback_payload",
    "validate_payload",
]
