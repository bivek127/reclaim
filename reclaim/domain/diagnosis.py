"""LLM diagnosis — classify failure, persist diagnosis, enter POLICY_EVAL.

The model diagnoses. It does not execute, determine amounts, or move money.
Financial attempt budget is never touched here.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import psycopg
from psycopg.types.json import Jsonb

from reclaim.config import lease_seconds_for, load_ollama_config
from reclaim.domain.leases import claim_next, fenced_transition
from reclaim.domain.states import CaseState
from reclaim.llm.client import (
    LlmClient,
    LlmEmptyResponse,
    LlmTimeout,
    LlmUnreachable,
    OllamaClient,
)
from reclaim.llm.fallback import fallback_payload
from reclaim.llm.prompt import (
    PROMPT_VERSION,
    TrustedDiagnosisContext,
    UntrustedDiagnosisContext,
    build_prompt,
)
from reclaim.llm.schema import (
    DiagnosisPayload,
    InvalidEnum,
    SchemaViolation,
    validate_payload,
)

SOURCE_LLM = "LLM"
SOURCE_FALLBACK = "DETERMINISTIC_FALLBACK"


class DiagnosisBlocked(Exception):
    """Case cannot be diagnosed as presented."""


@dataclass(frozen=True)
class DiagnosisResult:
    case_id: int
    case_state: CaseState
    applied: bool
    diagnosis_id: int | None = None
    source: str = ""
    cause: str = ""
    recommended_action: str | None = None
    llm_retry_count: int = 0
    reason: str = ""


def diagnose_case(
    conn: psycopg.Connection,
    case_id: int,
    *,
    llm: LlmClient,
    fencing_token: int,
    worker_id: str | None = None,
    untrusted: UntrustedDiagnosisContext | None = None,
) -> DiagnosisResult:
    """One diagnosis cycle. LLM call is outside the DB transaction."""
    trusted, failure_codes = load_trusted_context(conn, case_id)
    untrusted = untrusted or UntrustedDiagnosisContext()

    payload, source, model, retry_count, raw_response, reason = _invoke(
        llm,
        trusted=trusted,
        untrusted=untrusted,
        failure_codes=failure_codes,
    )

    with conn.transaction():
        if not _claimable(conn, case_id, fencing_token):
            fenced_transition(
                conn,
                case_id,
                CaseState.DIAGNOSING,
                CaseState.POLICY_EVAL,
                fencing_token,
                reason,
                worker_id=worker_id,
            )
            return DiagnosisResult(
                case_id=case_id,
                case_state=CaseState.DIAGNOSING,
                applied=False,
                reason=reason,
            )

        diagnosis_id = _insert_diagnosis(
            conn,
            case_id=case_id,
            source=source,
            model=model,
            payload=payload,
            llm_retry_count=retry_count,
            raw_response=raw_response,
        )

        def _settle(inner: psycopg.Connection) -> None:
            _release_lease(inner, case_id)
            _audit_diagnosis(
                inner,
                case_id=case_id,
                diagnosis_id=diagnosis_id,
                source=source,
                model=model,
                payload=payload,
                llm_retry_count=retry_count,
                worker_id=worker_id,
                fencing_token=fencing_token,
            )

        applied = fenced_transition(
            conn,
            case_id,
            CaseState.DIAGNOSING,
            CaseState.POLICY_EVAL,
            fencing_token,
            reason,
            worker_id=worker_id,
            side_effects=_settle,
        )
        if not applied:
            raise DiagnosisBlocked(
                f"diagnosis {diagnosis_id} inserted but transition rejected"
            )

    return DiagnosisResult(
        case_id=case_id,
        case_state=CaseState.POLICY_EVAL,
        applied=True,
        diagnosis_id=diagnosis_id,
        source=source,
        cause=payload.cause,
        recommended_action=payload.recommended_action,
        llm_retry_count=retry_count,
        reason=reason,
    )


def diagnose_once(
    conn: psycopg.Connection,
    *,
    llm: LlmClient | None = None,
    worker_id: str = "diagnosis",
    lease_seconds: int | None = None,
    untrusted: UntrustedDiagnosisContext | None = None,
) -> DiagnosisResult | None:
    """Claim one DIAGNOSING case and diagnose it. None when nothing claimable."""
    lease = lease_seconds or lease_seconds_for("diagnosis")
    claim = claim_next(conn, CaseState.DIAGNOSING, worker_id, lease)
    if claim is None:
        return None
    client = llm if llm is not None else OllamaClient(load_ollama_config())
    return diagnose_case(
        conn,
        claim.case_id,
        llm=client,
        fencing_token=claim.fencing_token,
        worker_id=worker_id,
        untrusted=untrusted,
    )


def load_trusted_context(
    conn: psycopg.Connection, case_id: int
) -> tuple[TrustedDiagnosisContext, tuple[str, ...]]:
    row = conn.execute(
        """
        SELECT o.amount_minor, o.currency, o.anchor_kind, c.attempt_count, c.state
          FROM recovery_cases c
          JOIN financial_obligations o ON o.id = c.obligation_id
         WHERE c.id = %s
        """,
        (case_id,),
    ).fetchone()
    if row is None:
        raise DiagnosisBlocked(f"case {case_id} not found")
    amount_minor, currency, anchor_kind, attempt_count, state = row
    if state != CaseState.DIAGNOSING.value:
        raise DiagnosisBlocked(f"case {case_id} is not in DIAGNOSING")

    codes = conn.execute(
        """
        SELECT COALESCE(pr.response_body #>> '{error,code}', pr.outcome::text)
          FROM provider_requests pr
          JOIN execution_attempts ea ON ea.id = pr.attempt_id
         WHERE ea.case_id = %s
         ORDER BY pr.id
        """,
        (case_id,),
    ).fetchall()
    failure_codes = tuple(str(r[0]) for r in codes if r[0])

    trusted = TrustedDiagnosisContext(
        amount_minor=int(amount_minor),
        currency=str(currency),
        anchor_kind=str(anchor_kind),
        attempt_count=int(attempt_count),
        failure_codes=failure_codes,
    )
    return trusted, failure_codes


def _invoke(
    llm: LlmClient,
    *,
    trusted: TrustedDiagnosisContext,
    untrusted: UntrustedDiagnosisContext,
    failure_codes: tuple[str, ...],
) -> tuple[DiagnosisPayload, str, str | None, int, Any, str]:
    """Run the diagnosis retry ladder. Never touches attempt_count."""
    system, prompt = build_prompt(trusted, untrusted, strict=False)
    retry_count = 0

    try:
        first = llm.complete(prompt, system=system)
    except LlmUnreachable:
        fb = fallback_payload(failure_codes)
        return fb, SOURCE_FALLBACK, None, 0, None, "diagnosis_fallback_unreachable"
    except LlmTimeout:
        retry_count = 1
        try:
            second = llm.complete(prompt, system=system)
            return _parse_or_fallback(
                second.text,
                model=second.model,
                failure_codes=failure_codes,
                retry_count=retry_count,
                raw=second.raw_body,
                allow_malformed_retry=False,
                trusted=trusted,
                untrusted=untrusted,
                llm=llm,
            )
        except (LlmUnreachable, LlmTimeout, LlmEmptyResponse):
            fb = fallback_payload(failure_codes)
            return fb, SOURCE_FALLBACK, None, retry_count, None, "diagnosis_fallback_timeout"
    except LlmEmptyResponse:
        retry_count = 1
        try:
            second = llm.complete(prompt, system=system)
            return _parse_or_fallback(
                second.text,
                model=second.model,
                failure_codes=failure_codes,
                retry_count=retry_count,
                raw=second.raw_body,
                allow_malformed_retry=False,
                trusted=trusted,
                untrusted=untrusted,
                llm=llm,
            )
        except (LlmUnreachable, LlmTimeout, LlmEmptyResponse):
            fb = fallback_payload(failure_codes)
            return fb, SOURCE_FALLBACK, None, retry_count, None, "diagnosis_fallback_empty"

    return _parse_or_fallback(
        first.text,
        model=first.model,
        failure_codes=failure_codes,
        retry_count=retry_count,
        raw=first.raw_body,
        allow_malformed_retry=True,
        trusted=trusted,
        untrusted=untrusted,
        llm=llm,
    )


def _parse_or_fallback(
    text: str,
    *,
    model: str,
    failure_codes: tuple[str, ...],
    retry_count: int,
    raw: Any,
    allow_malformed_retry: bool,
    trusted: TrustedDiagnosisContext,
    untrusted: UntrustedDiagnosisContext,
    llm: LlmClient,
) -> tuple[DiagnosisPayload, str, str | None, int, Any, str]:
    try:
        data = _parse_json(text)
    except json.JSONDecodeError:
        if allow_malformed_retry and retry_count < 1:
            system, prompt = build_prompt(trusted, untrusted, strict=True)
            try:
                second = llm.complete(prompt, system=system)
            except (LlmUnreachable, LlmTimeout, LlmEmptyResponse):
                fb = fallback_payload(failure_codes)
                return (
                    fb,
                    SOURCE_FALLBACK,
                    None,
                    1,
                    {"raw_text": text},
                    "diagnosis_fallback_malformed",
                )
            try:
                data = _parse_json(second.text)
                payload = validate_payload(data)
                return (
                    payload,
                    SOURCE_LLM,
                    second.model,
                    1,
                    second.raw_body or data,
                    "diagnosis_llm",
                )
            except (json.JSONDecodeError, SchemaViolation):
                fb = fallback_payload(failure_codes)
                return (
                    fb,
                    SOURCE_FALLBACK,
                    None,
                    1,
                    {"raw_text": second.text},
                    "diagnosis_fallback_malformed",
                )
        fb = fallback_payload(failure_codes)
        return (
            fb,
            SOURCE_FALLBACK,
            None,
            retry_count,
            {"raw_text": text},
            "diagnosis_fallback_malformed",
        )

    try:
        payload = validate_payload(data)
    except (SchemaViolation, InvalidEnum):
        # A structurally invalid response is not worth retrying: the model
        # already had one attempt at valid JSON with a valid enum value.
        fb = fallback_payload(failure_codes)
        return (
            fb,
            SOURCE_FALLBACK,
            None,
            retry_count,
            data,
            "diagnosis_fallback_schema",
        )

    return payload, SOURCE_LLM, model, retry_count, raw or data, "diagnosis_llm"


def _parse_json(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        raise json.JSONDecodeError("empty", text, 0)
    return json.loads(stripped)


def _claimable(conn: psycopg.Connection, case_id: int, fencing_token: int) -> bool:
    row = conn.execute(
        """
        SELECT 1 FROM recovery_cases
         WHERE id = %s AND state = %s AND fencing_token = %s
         FOR UPDATE
        """,
        (case_id, CaseState.DIAGNOSING.value, fencing_token),
    ).fetchone()
    return row is not None


def _insert_diagnosis(
    conn: psycopg.Connection,
    *,
    case_id: int,
    source: str,
    model: str | None,
    payload: DiagnosisPayload,
    llm_retry_count: int,
    raw_response: Any,
) -> int:
    row = conn.execute(
        """
        INSERT INTO diagnoses (
            case_id, source, model, prompt_version, cause, recommended_action,
            reasoning, confidence, raw_response, llm_retry_count
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id
        """,
        (
            case_id,
            source,
            model,
            PROMPT_VERSION,
            payload.cause,
            payload.recommended_action,
            payload.reasoning,
            payload.confidence,
            Jsonb(raw_response) if raw_response is not None else None,
            llm_retry_count,
        ),
    ).fetchone()
    assert row is not None
    return int(row[0])


def _release_lease(conn: psycopg.Connection, case_id: int) -> None:
    conn.execute(
        """
        UPDATE recovery_cases
           SET worker_id = NULL,
               lease_expires_at = '-infinity',
               updated_at = now()
         WHERE id = %s
        """,
        (case_id,),
    )


def _audit_diagnosis(
    conn: psycopg.Connection,
    *,
    case_id: int,
    diagnosis_id: int,
    source: str,
    model: str | None,
    payload: DiagnosisPayload,
    llm_retry_count: int,
    worker_id: str | None,
    fencing_token: int,
) -> None:
    conn.execute(
        """
        INSERT INTO audit_events (
            event_type, obligation_id, case_id, worker_id, fencing_token,
            reason_code, model, detail
        )
        SELECT 'diagnosis_produced', c.obligation_id, %s, %s, %s, %s, %s, %s
          FROM recovery_cases c WHERE c.id = %s
        """,
        (
            case_id,
            worker_id,
            fencing_token,
            "diagnosis_produced",
            model,
            Jsonb(
                {
                    "diagnosis_id": diagnosis_id,
                    "source": source,
                    "cause": payload.cause,
                    "recommended_action": payload.recommended_action,
                    "llm_retry_count": llm_retry_count,
                    "prompt_version": PROMPT_VERSION,
                }
            ),
            case_id,
        ),
    )
