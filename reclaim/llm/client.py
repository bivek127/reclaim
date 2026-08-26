"""Ollama client boundary. Domain code depends on LlmClient, not Ollama."""

from __future__ import annotations

import http.client
import json
import socket
from dataclasses import dataclass
from typing import Protocol


class LlmError(Exception):
    """Base class for LLM client failures."""


class LlmUnreachable(LlmError):
    """Ollama is down or the connection failed. Immediate fallback."""


class LlmTimeout(LlmError):
    """Call exceeded the configured timeout budget. One retry, then fallback."""


class LlmEmptyResponse(LlmError):
    """Empty / whitespace body. One retry, then fallback."""


@dataclass(frozen=True)
class LlmResponse:
    text: str
    model: str
    raw_body: dict | None = None


class LlmClient(Protocol):
    def complete(self, prompt: str, *, system: str | None = None) -> LlmResponse: ...


@dataclass(frozen=True)
class OllamaConfig:
    host: str = "127.0.0.1"
    port: int = 11434
    model: str = "gemma3:12b"
    timeout_seconds: int = 20


class OllamaClient:
    """Minimal Ollama `/api/generate` client. No internal retries — the caller owns them."""

    def __init__(self, config: OllamaConfig) -> None:
        self.config = config

    def complete(self, prompt: str, *, system: str | None = None) -> LlmResponse:
        payload: dict = {
            "model": self.config.model,
            "prompt": prompt if system is None else f"{system}\n\n{prompt}",
            "stream": False,
            "format": "json",
            "options": {"temperature": 0},
        }
        body = json.dumps(payload).encode("utf-8")
        timeout = float(self.config.timeout_seconds)
        try:
            conn = http.client.HTTPConnection(
                self.config.host, self.config.port, timeout=timeout
            )
            try:
                conn.request(
                    "POST",
                    "/api/generate",
                    body=body,
                    headers={"Content-Type": "application/json"},
                )
                response = conn.getresponse()
                raw = response.read()
                status = response.status
            finally:
                conn.close()
        except (TimeoutError, socket.timeout) as exc:
            raise LlmTimeout(str(exc)) from exc
        except (ConnectionRefusedError, ConnectionResetError, OSError) as exc:
            raise LlmUnreachable(str(exc)) from exc
        except http.client.HTTPException as exc:
            raise LlmUnreachable(str(exc)) from exc

        if status >= 500 or status == 404:
            raise LlmUnreachable(f"ollama HTTP {status}")
        if status >= 400:
            raise LlmUnreachable(f"ollama HTTP {status}: {raw[:200]!r}")

        try:
            parsed = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise LlmEmptyResponse(f"unparseable ollama envelope: {exc}") from exc

        text = parsed.get("response")
        if not isinstance(text, str) or not text.strip():
            raise LlmEmptyResponse("empty ollama response")

        return LlmResponse(text=text, model=self.config.model, raw_body=parsed)


class UnreachableLlm:
    """Test double: Ollama is down."""

    def complete(self, prompt: str, *, system: str | None = None) -> LlmResponse:
        raise LlmUnreachable("ollama unavailable (test double)")


class ScriptedLlm:
    """Test double: returns scripted responses/errors in order."""

    def __init__(self, *responses: str | BaseException, model: str = "test-model") -> None:
        self._responses: list[str | BaseException] = list(responses)
        self._model = model
        self.calls: list[str] = []

    def complete(self, prompt: str, *, system: str | None = None) -> LlmResponse:
        self.calls.append(prompt)
        if not self._responses:
            raise LlmEmptyResponse("scripted llm exhausted")
        item = self._responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        if not str(item).strip():
            raise LlmEmptyResponse("empty scripted response")
        return LlmResponse(text=str(item), model=self._model)
