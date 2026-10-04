from __future__ import annotations
import json, os
from urllib.request import Request, urlopen
from changeguard.llm.config import GeminiConfig
from changeguard.llm.ollama import OllamaError

class GeminiLLMAdapter:
    def __init__(self, config: GeminiConfig | None = None) -> None:
        self.config = config or GeminiConfig.from_env(); self._requests = 0
    def generate(self, prompt: str) -> str:
        key = os.environ.get("GEMINI_API_KEY")
        if not key: raise OllamaError("Gemini is not configured: set GEMINI_API_KEY locally")
        if self._requests >= self.config.max_requests: raise OllamaError("Gemini request budget exhausted for this analysis")
        self._requests += 1
        body=json.dumps({"contents":[{"parts":[{"text":prompt[:12000]}]}],"generationConfig":{"maxOutputTokens":self.config.max_output_tokens,"temperature":0}}).encode()
        request=Request(f"https://generativelanguage.googleapis.com/v1beta/models/{self.config.model}:generateContent",data=body,headers={"Content-Type":"application/json","x-goog-api-key":key},method="POST")
        try:
            with urlopen(request,timeout=self.config.timeout_seconds) as response: payload=json.loads(response.read().decode())
            text=payload["candidates"][0]["content"]["parts"][0]["text"]
        except Exception as error: raise OllamaError(f"Gemini request failed: {error}") from error
        if not isinstance(text,str): raise OllamaError("Gemini returned a non-text response")
        return text
