from __future__ import annotations

from .config import LLM_PROVIDERS
from .gemini import GeminiLLMAdapter
from .ollama import OllamaLLMAdapter
from .port import LLMPort

# Gemini is a paid remote API, so its investigation is capped at one tool call.
GEMINI_MAX_TOOL_CALLS = 1


def create_llm_adapter(provider: str) -> LLMPort:
    """Build the LLM adapter for a provider name ("ollama" or "gemini")."""
    if provider == "gemini":
        return GeminiLLMAdapter()
    if provider == "ollama":
        return OllamaLLMAdapter()
    raise ValueError(f"Unknown LLM provider {provider!r}; expected one of: {', '.join(LLM_PROVIDERS)}")


def max_tool_calls_for(provider: str, requested: int) -> int:
    """Apply the provider's tool-call cap to the requested budget."""
    return min(requested, GEMINI_MAX_TOOL_CALLS) if provider == "gemini" else requested
