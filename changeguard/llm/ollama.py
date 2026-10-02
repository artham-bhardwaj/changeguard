from __future__ import annotations

import json
import socket
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from changeguard.llm.config import OllamaConfig


class OllamaError(RuntimeError):
    """Base error for failures communicating with the local Ollama service."""


class OllamaConnectionError(OllamaError):
    """Ollama could not be reached."""


class OllamaTimeoutError(OllamaError):
    """Ollama did not respond before the configured timeout."""


class OllamaModelUnavailableError(OllamaError):
    """The requested model is not available in Ollama."""


class OllamaRequestError(OllamaError):
    """Ollama rejected the request or returned an unusable response."""


class OllamaLLMAdapter:
    def __init__(self, config: OllamaConfig | None = None) -> None:
        self.config = config or OllamaConfig.from_env()

    def generate(self, prompt: str) -> str:
        body = json.dumps(
            {"model": self.config.model, "prompt": prompt, "stream": False}
        ).encode("utf-8")
        request = Request(
            f"{self.config.base_url}/api/generate",
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urlopen(request, timeout=self.config.timeout_seconds) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            try:
                details = error.read().decode("utf-8", errors="replace").strip()
            except (socket.timeout, TimeoutError) as read_error:
                raise OllamaTimeoutError(
                    f"Ollama did not respond within "
                    f"{self.config.timeout_seconds:g} seconds"
                ) from read_error
            except OSError as read_error:
                raise OllamaConnectionError(
                    f"Connection to Ollama at {self.config.base_url} was interrupted"
                ) from read_error
            if error.code == 404:
                raise OllamaModelUnavailableError(
                    f"Ollama model '{self.config.model}' is unavailable"
                    + (f": {details}" if details else "")
                ) from error
            raise OllamaRequestError(
                f"Ollama returned HTTP {error.code}"
                + (f": {details}" if details else "")
            ) from error
        except (socket.timeout, TimeoutError) as error:
            raise OllamaTimeoutError(
                f"Ollama did not respond within "
                f"{self.config.timeout_seconds:g} seconds"
            ) from error
        except URLError as error:
            if isinstance(error.reason, (socket.timeout, TimeoutError)):
                raise OllamaTimeoutError(
                    f"Ollama did not respond within "
                    f"{self.config.timeout_seconds:g} seconds"
                ) from error
            raise OllamaConnectionError(
                f"Could not connect to Ollama at {self.config.base_url}: "
                f"{error.reason}"
            ) from error
        except OSError as error:
            raise OllamaConnectionError(
                f"Could not connect to Ollama at {self.config.base_url}: {error}"
            ) from error
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise OllamaRequestError("Ollama returned malformed JSON") from error

        if not isinstance(payload, dict):
            raise OllamaRequestError("Ollama response must be a JSON object")
        if isinstance(payload.get("error"), str):
            message = payload["error"]
            if "model" in message.lower() and (
                "not found" in message.lower() or "unavailable" in message.lower()
            ):
                raise OllamaModelUnavailableError(
                    f"Ollama model '{self.config.model}' is unavailable: {message}"
                )
            raise OllamaRequestError(f"Ollama error: {message}")

        generated = payload.get("response")
        if not isinstance(generated, str):
            raise OllamaRequestError(
                "Ollama response omitted a string 'response' field"
            )
        return generated
