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


GEMINI_THINKING_LEVELS = ("minimal", "low", "medium", "high")


@dataclass(frozen=True)
class GeminiConfig:
    model: str = "gemini-3.5-flash"
    timeout_seconds: float = 30.0
    max_requests: int = 3
    max_output_tokens: int = 800
    # Thinking tokens count against max_output_tokens, so keep thinking low by default.
    # None omits thinkingConfig (needed for models without thinking levels).
    thinking_level: str | None = "low"
    # Retries for temporary API errors (429/5xx/network); retries do not use the request budget.
    max_retries: int = 2
    retry_delay_seconds: float = 2.0

    def __post_init__(self) -> None:
        if not self.model.strip():
            raise ValueError("Gemini model name must not be empty")
        if not math.isfinite(self.timeout_seconds) or self.timeout_seconds <= 0:
            raise ValueError("Gemini timeout must be greater than zero")
        if not 1 <= self.max_requests <= 3:
            raise ValueError("Gemini max requests must be between 1 and 3")
        if not 1 <= self.max_output_tokens <= 1200:
            raise ValueError("Gemini max output tokens must be between 1 and 1200")
        if self.thinking_level is not None and self.thinking_level not in GEMINI_THINKING_LEVELS:
            raise ValueError(
                f"Gemini thinking level must be one of: {', '.join(GEMINI_THINKING_LEVELS)}, or empty"
            )
        if not 0 <= self.max_retries <= 5:
            raise ValueError("Gemini max retries must be between 0 and 5")
        if not math.isfinite(self.retry_delay_seconds) or self.retry_delay_seconds < 0:
            raise ValueError("Gemini retry delay must not be negative")

    @classmethod
    def from_env(cls) -> "GeminiConfig":
        def number(name: str, default: str, convert):
            raw = os.getenv(name, default)
            try:
                return convert(raw)
            except ValueError as error:
                raise ValueError(f"{name} must be a number, got {raw!r}") from error

        thinking = os.getenv("CHANGEGUARD_GEMINI_THINKING_LEVEL", "low").strip().lower()
        return cls(
            model=os.getenv("CHANGEGUARD_GEMINI_MODEL", "gemini-3.5-flash"),
            timeout_seconds=number("CHANGEGUARD_GEMINI_TIMEOUT_SECONDS", "30", float),
            max_requests=number("CHANGEGUARD_GEMINI_MAX_REQUESTS", "3", int),
            max_output_tokens=number("CHANGEGUARD_GEMINI_MAX_OUTPUT_TOKENS", "800", int),
            thinking_level=thinking or None,
            max_retries=number("CHANGEGUARD_GEMINI_MAX_RETRIES", "2", int),
        )


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


def user_env_file() -> Path:
    """User-wide settings file: $XDG_CONFIG_HOME/changeguard/.env (default ~/.config)."""
    config_home = os.getenv("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    return Path(config_home) / "changeguard" / ".env"


def load_local_env(path: str | os.PathLike[str] | None = None) -> None:
    """Load KEY=VALUE pairs into the environment without overriding existing variables.

    Uses ``path`` if given. Otherwise loads ``CHANGEGUARD_ENV_FILE`` or the nearest ``.env``
    in the current directory or its parents (up to the git root), then the user-wide
    ``~/.config/changeguard/.env``; earlier files win.
    """
    if path is not None:
        _load_env_file(Path(path))
        return
    configured = os.getenv("CHANGEGUARD_ENV_FILE")
    project_file = Path(configured) if configured else find_env_file()
    if project_file is not None:
        _load_env_file(project_file)
    _load_env_file(user_env_file())


def _load_env_file(env_file: Path) -> None:
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
        if separator and name and name not in os.environ:
            os.environ[name] = _parse_env_value(value)
            _ENV_SOURCES[name] = str(env_file)


_ENV_SOURCES: dict[str, str] = {}


def env_source(name: str) -> str | None:
    """Where a setting came from: the .env file that set it, "environment", or None if unset."""
    if name in _ENV_SOURCES and os.environ.get(name) is not None:
        return _ENV_SOURCES[name]
    return "environment" if name in os.environ else None
