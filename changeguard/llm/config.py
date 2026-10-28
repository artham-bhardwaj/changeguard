from __future__ import annotations

import math
import os
from dataclasses import dataclass
from pathlib import Path
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


@dataclass(frozen=True)
class GeminiConfig:
    model: str = "gemini-3.5-flash"
    timeout_seconds: float = 30.0
    max_requests: int = 3
    max_output_tokens: int = 800

    def __post_init__(self) -> None:
        if not self.model.strip(): raise ValueError("Gemini model name must not be empty")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0: raise ValueError("Gemini timeout must be greater than zero")
        if not 1 <= self.max_requests <= 3: raise ValueError("Gemini max requests must be between 1 and 3")
        if not 1 <= self.max_output_tokens <= 1200: raise ValueError("Gemini max output tokens must be between 1 and 1200")

    @classmethod
    def from_env(cls) -> "GeminiConfig":
        return cls(os.getenv("CHANGEGUARD_GEMINI_MODEL", "gemini-3.5-flash"), float(os.getenv("CHANGEGUARD_GEMINI_TIMEOUT_SECONDS", "30")), int(os.getenv("CHANGEGUARD_GEMINI_MAX_REQUESTS", "3")), int(os.getenv("CHANGEGUARD_GEMINI_MAX_OUTPUT_TOKENS", "800")))


LLM_PROVIDERS = ("ollama", "gemini")


def default_llm_provider() -> str:
    """Return the LLM provider chosen by CHANGEGUARD_LLM_PROVIDER (default: ollama)."""
    provider = os.getenv("CHANGEGUARD_LLM_PROVIDER", "ollama").strip().lower() or "ollama"
    if provider not in LLM_PROVIDERS:
        raise ValueError(
            f"CHANGEGUARD_LLM_PROVIDER must be one of: {', '.join(LLM_PROVIDERS)}"
        )
    return provider


def find_env_file(start: str | os.PathLike[str] | None = None) -> Path | None:
    """Find a .env file in the start directory or its parents, stopping at a git root."""
    directory = Path(start or Path.cwd()).resolve()
    for candidate in (directory, *directory.parents):
        env_file = candidate / ".env"
        if env_file.is_file():
            return env_file
        if (candidate / ".git").exists():
            return None
    return None


def _parse_env_value(raw: str) -> str:
    value = raw.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    comment = value.find(" #")
    return value[:comment].rstrip() if comment >= 0 else value


def load_local_env(path: str | os.PathLike[str] | None = None) -> None:
    """Load KEY=VALUE pairs into the environment without overriding existing variables.

    Uses ``path`` if given, else ``CHANGEGUARD_ENV_FILE``, else the nearest ``.env``
    in the current directory or its parents (up to the git root).
    """
    if path is None:
        configured = os.getenv("CHANGEGUARD_ENV_FILE")
        env_file = Path(configured) if configured else find_env_file()
        if env_file is None:
            return
    else:
        env_file = Path(path)
    try:
        lines = env_file.read_text(encoding="utf-8").splitlines()
    except FileNotFoundError:
        return
    for line in lines:
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, separator, value = line.partition("=")
        name = name.strip()
        if separator and name:
            os.environ.setdefault(name, _parse_env_value(value))
