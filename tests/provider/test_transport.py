"""The stdlib transport against a real localhost socket.

These exercise the actual `http.client` code path rather than a stub, because the
zero-bytes-written distinction is the one thing that must be observed rather than
inferred. No internet access is involved.
"""

from __future__ import annotations

import http.client
import socket
import threading
import time
from typing import Iterator

import pytest

from reclaim.provider.transport import (
    HttpClientTransport,
    TransportFailure,
    TransportPhase,
)


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class _Server:
    """Accepts one connection and behaves as instructed."""

    def __init__(self, *, reply: bytes | None, delay: float = 0.0) -> None:
        self._reply = reply
        self._delay = delay
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(1)
        self.port = self._sock.getsockname()[1]
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self) -> None:
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        with conn:
            try:
                conn.recv(65536)
                if self._delay:
                    time.sleep(self._delay)
                if self._reply is not None:
                    conn.sendall(self._reply)
            except OSError:
                pass

    def close(self) -> None:
        try:
            self._sock.close()
        except OSError:
            pass
        self._thread.join(timeout=2)


@pytest.fixture
def plain_transport_factory():
    servers: list[_Server] = []

    def factory(*, reply: bytes | None, delay: float = 0.0) -> HttpClientTransport:
        server = _Server(reply=reply, delay=delay)
        servers.append(server)
        return HttpClientTransport(
            f"127.0.0.1:{server.port}",
            connection_factory=http.client.HTTPConnection,
        )

    yield factory
    for server in servers:
        server.close()


def _get(transport: HttpClientTransport, *, read_timeout: float = 2.0):
    return transport.request(
        "GET",
        "/v1/payment_links",
        headers={"Accept": "application/json"},
        connect_timeout=2.0,
        read_timeout=read_timeout,
    )


def _http_reply(body: bytes, *, status_line: bytes = b"HTTP/1.1 200 OK") -> bytes:
    return b"\r\n".join(
        [
            status_line,
            b"Content-Type: application/json",
            b"Content-Length: " + str(len(body)).encode("ascii"),
            b"",
            body,
        ]
    )


def test_successful_round_trip_returns_status_and_body(plain_transport_factory) -> None:
    transport = plain_transport_factory(reply=_http_reply(b'{"payment_links": []}'))

    response = _get(transport)

    assert response.status == 200
    assert response.body == b'{"payment_links": []}'
    assert response.headers["content-type"] == "application/json"


def test_connection_refused_is_the_connect_phase(plain_transport_factory) -> None:
    """Nothing was written, so this resolves as TRANSPORT_ERROR rather than ambiguity."""
    transport = HttpClientTransport(
        f"127.0.0.1:{_free_port()}",
        connection_factory=http.client.HTTPConnection,
    )

    with pytest.raises(TransportFailure) as excinfo:
        _get(transport)

    assert excinfo.value.phase is TransportPhase.CONNECT
    assert excinfo.value.bytes_written is False


def test_server_that_never_replies_is_the_read_phase(plain_transport_factory) -> None:
    """Bytes reached the provider, so the outcome must stay unknown."""
    transport = plain_transport_factory(reply=None, delay=5.0)

    with pytest.raises(TransportFailure) as excinfo:
        _get(transport, read_timeout=0.3)

    assert excinfo.value.phase is TransportPhase.READ
    assert excinfo.value.timed_out is True
    assert excinfo.value.bytes_written is True


def test_read_timeout_does_not_use_the_connect_timeout(plain_transport_factory) -> None:
    transport = plain_transport_factory(reply=None, delay=5.0)
    started = time.monotonic()

    with pytest.raises(TransportFailure):
        _get(transport, read_timeout=0.3)

    assert time.monotonic() - started < 2.0


def test_failure_message_carries_no_credentials(plain_transport_factory) -> None:
    transport = HttpClientTransport(
        f"127.0.0.1:{_free_port()}",
        connection_factory=http.client.HTTPConnection,
    )

    with pytest.raises(TransportFailure) as excinfo:
        transport.request(
            "POST",
            "/v1/payment_links",
            headers={"Authorization": "Basic c2VjcmV0"},
            body=b"{}",
            connect_timeout=2.0,
            read_timeout=2.0,
        )

    rendered = str(excinfo.value)
    assert "Authorization" not in rendered
    assert "c2VjcmV0" not in rendered


def test_transport_performs_no_internal_retries(plain_transport_factory) -> None:
    """The caller owns retry orchestration; the transport makes exactly one attempt."""
    server = _Server(reply=None, delay=5.0)
    try:
        transport = HttpClientTransport(
            f"127.0.0.1:{server.port}",
            connection_factory=http.client.HTTPConnection,
        )
        started = time.monotonic()
        with pytest.raises(TransportFailure):
            _get(transport, read_timeout=0.3)
        assert time.monotonic() - started < 1.5
    finally:
        server.close()
