import json
import pytest
from changeguard.llm.config import GeminiConfig
from changeguard.llm.gemini import GeminiLLMAdapter
from changeguard.llm.ollama import OllamaError

class Response:
    def __enter__(self): return self
    def __exit__(self,*args): return None
    def read(self): return b'{"candidates":[{"content":{"parts":[{"text":"report"}]}}]}'

def test_gemini_request_is_bounded(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY","test")
    seen={}
    def fake(request,timeout): seen["body"]=json.loads(request.data); return Response()
    monkeypatch.setattr("changeguard.llm.gemini.urlopen",fake)
    adapter=GeminiLLMAdapter(GeminiConfig(model="test",max_requests=1,max_output_tokens=20))
    assert adapter.generate("x"*13000)=="report"
    assert len(seen["body"]["contents"][0]["parts"][0]["text"])==12000
    with pytest.raises(OllamaError,match="budget exhausted"): adapter.generate("again")


import io
from urllib.error import HTTPError


class JSONResponse:
    def __init__(self, payload): self.payload = payload
    def __enter__(self): return self
    def __exit__(self, *args): return None
    def read(self): return json.dumps(self.payload).encode()


def _candidate(parts, finish="STOP"):
    return {"candidates": [{"content": {"parts": parts}, "finishReason": finish}]}


def _adapter(monkeypatch, responses, **config):
    monkeypatch.setenv("GEMINI_API_KEY", "test")
    calls = []
    def fake(request, timeout):
        calls.append(json.loads(request.data))
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return JSONResponse(item)
    monkeypatch.setattr("changeguard.llm.gemini.urlopen", fake)
    adapter = GeminiLLMAdapter(GeminiConfig(model="test", **config), sleep=lambda _s: None)
    return adapter, calls


def _http_error(code):
    body = io.BytesIO(json.dumps({"error": {"message": f"status {code}"}}).encode())
    return HTTPError("https://example.invalid", code, "error", {}, body)


def test_gemini_joins_split_parts_and_skips_thoughts(monkeypatch):
    adapter, calls = _adapter(monkeypatch, [_candidate([
        {"text": "thinking...", "thought": True},
        {"text": '{"summary": "a', "thoughtSignature": "sig"},
        {"text": ' b"}'},
    ])])
    assert json.loads(adapter.generate("prompt")) == {"summary": "a b"}
    generation = calls[0]["generationConfig"]
    assert generation["responseMimeType"] == "application/json"
    assert generation["thinkingConfig"] == {"thinkingLevel": "low"}


def test_gemini_thinking_config_can_be_disabled(monkeypatch):
    adapter, calls = _adapter(monkeypatch, [_candidate([{"text": "{}"}])], thinking_level=None)
    adapter.generate("prompt")
    assert "thinkingConfig" not in calls[0]["generationConfig"]


def test_gemini_strips_markdown_code_fence(monkeypatch):
    adapter, _ = _adapter(monkeypatch, [_candidate([{"text": '```json\n{"a": 1}\n```'}])])
    assert adapter.generate("prompt") == '{"a": 1}'


def test_gemini_reports_truncated_output(monkeypatch):
    adapter, _ = _adapter(monkeypatch, [_candidate([{"text": '{"a":'}], finish="MAX_TOKENS")])
    with pytest.raises(OllamaError, match="output token limit"):
        adapter.generate("prompt")


def test_gemini_retries_temporary_errors_without_using_budget(monkeypatch):
    adapter, calls = _adapter(
        monkeypatch, [_http_error(503), _http_error(429), _candidate([{"text": "{}"}])], max_requests=1
    )
    assert adapter.generate("prompt") == "{}"
    assert len(calls) == 3
    with pytest.raises(OllamaError, match="budget exhausted"):
        adapter.generate("again")


def test_gemini_does_not_retry_client_errors(monkeypatch):
    adapter, calls = _adapter(monkeypatch, [_http_error(400)])
    with pytest.raises(OllamaError, match="HTTP 400: status 400"):
        adapter.generate("prompt")
    assert len(calls) == 1


def test_gemini_gives_up_after_max_retries(monkeypatch):
    adapter, calls = _adapter(monkeypatch, [_http_error(503)] * 3, max_retries=2)
    with pytest.raises(OllamaError, match="HTTP 503"):
        adapter.generate("prompt")
    assert len(calls) == 3


def test_gemini_requires_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(OllamaError, match="GEMINI_API_KEY"):
        GeminiLLMAdapter(GeminiConfig(model="test"))
