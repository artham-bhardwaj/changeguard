from __future__ import annotations

from changeguard.llm.ollama import OllamaLLMAdapter


def main() -> None:
    response = OllamaLLMAdapter().generate("Reply with exactly: ChangeGuard Ollama OK")
    print(response)


if __name__ == "__main__":
    main()
