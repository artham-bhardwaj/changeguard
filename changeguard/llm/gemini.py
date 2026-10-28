from __future__ import annotations

import json
import os
import time
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from changeguard.llm.config import GeminiConfig
from changeguard.llm.ollama import OllamaError

MAX_PROMPT_CHARS = 12000
_API_URL = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class GeminiLLMAdapter:
    """Bounded Gemini generateContent client. Every ChangeGuard prompt expects JSON output."""

    def __init__(self, config: GeminiConfig | None = None, *, sleep=time.sleep) -> None:
        self.config = config or GeminiConfig.from_env()
        self._requests = 0
        self._sleep = sleep
        if not os.environ.get("GEMINI_API_KEY"):
            raise OllamaError("Gemini is not configured: set GEMINI_API_KEY in .env or the environment")

    def generate(self, prompt: str) -> str:
        key = os.environ.get("GEMINI_API_KEY")
        if not key:
            raise OllamaError("Gemini is not configured: set GEMINI_API_KEY in .env or the environment")
        if self._requests >= self.config.max_requests:
            raise OllamaError("Gemini request budget exhausted for this analysis")
        self._requests += 1
        payload = self._post(self._request_body(prompt), key)
        return self._extract_text(payload)

    def _request_body(self, prompt: str) -> bytes:
        generation_config: dict[str, Any] = {
            "maxOutputTokens": self.config.max_output_tokens,
            "temperature": 0,
            "responseMimeType": "application/json",
        }
        if self.config.thinking_level:
            generation_config["thinkingConfig"] = {"thinkingLevel": self.config.thinking_level}
        return json.dumps(
            {
                "contents": [{"parts": [{"text": prompt[:MAX_PROMPT_CHARS]}]}],
                "generationConfig": generation_config,
            }
        ).encode()

    def _post(self, body: bytes, key: str) -> dict[str, Any]:
        request = Request(
            _API_URL.format(model=self.config.model),
            data=body,
            headers={"Content-Type": "application/json", "x-goog-api-key": key},
            method="POST",
        )
        attempt = 0
        while True:
            try:
                with urlopen(request, timeout=self.config.timeout_seconds) as response:
                    return json.loads(response.read().decode())
            except HTTPError as error:
                retryable = error.code in _RETRYABLE_STATUS
                message = f"HTTP {error.code}: {_api_error_message(error)}"
            except (URLError, TimeoutError) as error:
                retryable = True
                message = str(getattr(error, "reason", error))
            except (ValueError, UnicodeDecodeError) as error:
                raise OllamaError(f"Gemini returned invalid JSON: {error}") from error
            if not retryable or attempt >= self.config.max_retries:
                raise OllamaError(f"Gemini request failed: {message}")
            attempt += 1
            self._sleep(self.config.retry_delay_seconds * attempt)

    @staticmethod
    def _extract_text(payload: dict[str, Any]) -> str:
        try:
            candidate = payload["candidates"][0]
        except (KeyError, IndexError, TypeError) as error:
            feedback = payload.get("promptFeedback") if isinstance(payload, dict) else None
            raise OllamaError(f"Gemini returned no candidates: {feedback or 'empty response'}") from error
        finish_reason = candidate.get("finishReason")
        parts = (candidate.get("content") or {}).get("parts") or []
        # Replies can be split across several parts; thought summaries are not part of the answer.
        text = "".join(
            part["text"] for part in parts
            if isinstance(part, dict) and isinstance(part.get("text"), str) and not part.get("thought")
        ).strip()
        if finish_reason == "MAX_TOKENS":
            raise OllamaError(
                "Gemini response was cut off by the output token limit "
                "(raise CHANGEGUARD_GEMINI_MAX_OUTPUT_TOKENS, max 1200)"
            )
        if not text:
            raise OllamaError(f"Gemini returned an empty response (finish reason: {finish_reason})")
        return _strip_code_fence(text)


def _strip_code_fence(text: str) -> str:
    if text.startswith("```") and text.endswith("```"):
        body = text[3:-3]
        newline = body.find("\n")
        first_line = body[:newline] if newline >= 0 else ""
        if newline >= 0 and first_line.strip().isalpha():
            body = body[newline + 1:]
        return body.strip()
    return text


def _api_error_message(error: HTTPError) -> str:
    try:
        detail = json.loads(error.read().decode())
        return str(detail["error"]["message"])[:300]
    except Exception:
        return str(error.reason)
