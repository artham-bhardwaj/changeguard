from __future__ import annotations

import json
import socket
from io import BytesIO
from urllib.error import HTTPError, URLError

import pytest

from changeguard.llm.config import OllamaConfig
from changeguard.llm.ollama import (
    OllamaConnectionError,
    OllamaLLMAdapter,
    OllamaModelUnavailableError,
    OllamaRequestError,
    OllamaTimeoutError,
)


class FakeHTTPResponse:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def __enter__(self) -> FakeHTTPResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.payload


def test_ollama_config_defaults_and_environment(monkeypatch):
    defaults = OllamaConfig.from_env()
    assert defaults.base_url == "http://127.0.0.1:11434"
    assert defaults.model == "qwen2.5:1.5b"
    assert defaults.timeout_seconds == 45

    monkeypatch.setenv("CHANGEGUARD_OLLAMA_BASE_URL", "http://localhost:1234/")
    monkeypatch.setenv("CHANGEGUARD_MODEL", "small-local-model")
    monkeypatch.setenv("CHANGEGUARD_OLLAMA_TIMEOUT_SECONDS", "8.5")
    configured = OllamaConfig.from_env()
    assert configured.base_url == "http://localhost:1234"
    assert configured.model == "small-local-model"
    assert configured.timeout_seconds == 8.5


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"base_url": "localhost:11434"}, "absolute HTTP or HTTPS"),
        ({"model": " "}, "model name must not be empty"),
        ({"timeout_seconds": 0}, "timeout must be greater than zero"),
        ({"timeout_seconds": float("nan")}, "timeout must be greater than zero"),
    ],
)
def test_ollama_config_rejects_invalid_values(kwargs, message):
    with pytest.raises(ValueError, match=message):
        OllamaConfig(**kwargs)


def test_generate_posts_prompt_to_ollama(monkeypatch):
    requested = {}

    def fake_urlopen(request, timeout):
        requested["url"] = request.full_url
        requested["body"] = json.loads(request.data)
        requested["timeout"] = timeout
        return FakeHTTPResponse(b'{"response":"Generated locally"}')

    monkeypatch.setattr("changeguard.llm.ollama.urlopen", fake_urlopen)
    adapter = OllamaLLMAdapter(OllamaConfig(model="test-model", timeout_seconds=3))

    assert adapter.generate("Summarize this change") == "Generated locally"
    assert requested == {
        "url": "http://127.0.0.1:11434/api/generate",
        "body": {
            "model": "test-model",
            "prompt": "Summarize this change",
            "stream": False,
        },
        "timeout": 3,
    }


def test_generate_reports_connection_failure(monkeypatch):
    def fail_to_connect(request, timeout):
        raise URLError("connection refused")

    monkeypatch.setattr("changeguard.llm.ollama.urlopen", fail_to_connect)
    with pytest.raises(OllamaConnectionError, match="Could not connect to Ollama"):
        OllamaLLMAdapter().generate("prompt")


def test_generate_reports_timeout(monkeypatch):
    def timeout(request, timeout):
        raise socket.timeout()

    monkeypatch.setattr("changeguard.llm.ollama.urlopen", timeout)
    with pytest.raises(OllamaTimeoutError, match="45 seconds"):
        OllamaLLMAdapter().generate("prompt")


def test_generate_reports_missing_model(monkeypatch):
    def missing_model(request, timeout):
        raise HTTPError(
            request.full_url,
            404,
            "Not Found",
            {},
            BytesIO(b'{"error":"model test-model not found"}'),
        )

    monkeypatch.setattr("changeguard.llm.ollama.urlopen", missing_model)
    with pytest.raises(OllamaModelUnavailableError, match="qwen2.5:1.5b"):
        OllamaLLMAdapter().generate("prompt")


@pytest.mark.parametrize(
    "payload",
    [b"not-json", b"[]", b"{}", b'{"response": false}'],
)
def test_generate_reports_malformed_response(monkeypatch, payload):
    monkeypatch.setattr(
        "changeguard.llm.ollama.urlopen",
        lambda request, timeout: FakeHTTPResponse(payload),
    )
    with pytest.raises(OllamaRequestError):
        OllamaLLMAdapter().generate("prompt")
