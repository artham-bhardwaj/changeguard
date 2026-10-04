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
