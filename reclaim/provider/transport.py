"""stdlib HTTP transport that can prove whether any bytes reached the provider.

A zero-bytes-written failure is TRANSPORT_ERROR rather than ambiguity, but only
when the client can actually prove it -- otherwise it must fall back to
ambiguity. `http.client` lets connect() run as its own step, so the proof is
observed rather than inferred from an exception class.
"""

from __future__ import annotations

import http.client
import socket
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Protocol, TypeAlias

ConnectionFactory: TypeAlias = Callable[..., http.client.HTTPConnection]


class TransportPhase(str, Enum):
    """Where the call died. Only CONNECT proves nothing was written."""

    CONNECT = "CONNECT"
    SEND = "SEND"
    READ = "READ"


@dataclass(frozen=True)
class HttpResponse:
    status: int
    body: bytes
    headers: dict[str, str]


class TransportFailure(Exception):
    def __init__(self, phase: TransportPhase, *, timed_out: bool, detail: str) -> None:
        self.phase = phase
        self.timed_out = timed_out
        self.detail = detail
        super().__init__(f"{phase.value} failed: {detail}")

    @property
    def bytes_written(self) -> bool:
        return self.phase is not TransportPhase.CONNECT


class Transport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        body: bytes | None = None,
        connect_timeout: float,
        read_timeout: float,
    ) -> HttpResponse: ...


def _detail(exc: BaseException) -> str:
    """Exception text only. Never a header, never a body, never a credential."""
    return f"{type(exc).__name__}: {exc}"


class HttpClientTransport:
    """One connection per request. No pooling, no internal retries -- the caller owns retry."""

    def __init__(
        self,
        host: str,
        *,
        connection_factory: ConnectionFactory | None = None,
    ) -> None:
        self.host = host
        self._connection_factory: ConnectionFactory = (
            connection_factory or http.client.HTTPSConnection
        )

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: dict[str, str],
        body: bytes | None = None,
        connect_timeout: float,
        read_timeout: float,
    ) -> HttpResponse:
        conn = self._connection_factory(self.host, timeout=connect_timeout)

        try:
            try:
                conn.connect()
            except socket.timeout as exc:
                raise TransportFailure(
                    TransportPhase.CONNECT, timed_out=True, detail=_detail(exc)
                ) from exc
            except OSError as exc:
                raise TransportFailure(
                    TransportPhase.CONNECT, timed_out=False, detail=_detail(exc)
                ) from exc

            # Connected. Anything from here on may have put bytes on the wire.
            if conn.sock is not None:
                conn.sock.settimeout(read_timeout)

            try:
                conn.request(method, path, body=body, headers=headers)
            except socket.timeout as exc:
                raise TransportFailure(
                    TransportPhase.SEND, timed_out=True, detail=_detail(exc)
                ) from exc
            except (OSError, http.client.HTTPException) as exc:
                raise TransportFailure(
                    TransportPhase.SEND, timed_out=False, detail=_detail(exc)
                ) from exc

            try:
                response = conn.getresponse()
                payload = response.read()
            except socket.timeout as exc:
                raise TransportFailure(
                    TransportPhase.READ, timed_out=True, detail=_detail(exc)
                ) from exc
            except (OSError, http.client.HTTPException) as exc:
                raise TransportFailure(
                    TransportPhase.READ, timed_out=False, detail=_detail(exc)
                ) from exc

            return HttpResponse(
                status=response.status,
                body=payload,
                headers={key.lower(): value for key, value in response.getheaders()},
            )
        finally:
            _close_quietly(conn)


def _close_quietly(conn: Any) -> None:
    try:
        conn.close()
    except OSError:
        pass
