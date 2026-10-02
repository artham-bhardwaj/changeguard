from __future__ import annotations

import math
import os
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class OllamaConfig:
    base_url: str = "http://127.0.0.1:11434"
    model: str = "qwen2.5:1.5b"
    timeout_seconds: float = 45.0

    def __post_init__(self) -> None:
        normalized_url = self.base_url.rstrip("/")
        parsed_url = urlsplit(normalized_url)
        if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
            raise ValueError("Ollama base URL must be an absolute HTTP or HTTPS URL")
        if not self.model.strip():
            raise ValueError("Ollama model name must not be empty")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("Ollama timeout must be greater than zero")
        object.__setattr__(self, "base_url", normalized_url)

    @classmethod
    def from_env(cls) -> OllamaConfig:
        timeout = os.getenv("CHANGEGUARD_OLLAMA_TIMEOUT_SECONDS", "45")
        try:
            timeout_seconds = float(timeout)
        except ValueError as error:
            raise ValueError(
                "CHANGEGUARD_OLLAMA_TIMEOUT_SECONDS must be a number"
            ) from error
        return cls(
            base_url=os.getenv(
                "CHANGEGUARD_OLLAMA_BASE_URL", "http://127.0.0.1:11434"
            ),
            model=os.getenv("CHANGEGUARD_MODEL", "qwen2.5:1.5b"),
            timeout_seconds=timeout_seconds,
        )
