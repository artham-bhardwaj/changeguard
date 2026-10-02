from __future__ import annotations
import json, os
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.error import URLError
from urllib.request import Request, urlopen
@dataclass(frozen=True)
class ToolCall:
    name: str
    arguments: dict[str, Any]
    call_id: str = ""
@dataclass(frozen=True)
class LLMResponse:
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    model: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
class LLMUnavailableError(RuntimeError): pass
class LLMClient(Protocol):
    def complete(self,messages: list[dict[str,Any]],tools: list[dict[str,Any]])->LLMResponse: ...
class OllamaLLMClient:
    def __init__(self,model: str|None=None,base_url: str="http://127.0.0.1:11434",timeout: float=45)->None:
        self.model=model or os.getenv("CHANGEGUARD_MODEL","qwen2.5:1.5b")
        self.base_url=base_url.rstrip("/"); self.timeout=timeout
    def complete(self,messages: list[dict[str,Any]],tools: list[dict[str,Any]])->LLMResponse:
        body=json.dumps({"model":self.model,"messages":messages,"tools":tools,"stream":False,"options":{"num_predict":1200}}).encode()
        request=Request(self.base_url+"/api/chat",data=body,headers={"Content-Type":"application/json"},method="POST")
        try:
            with urlopen(request,timeout=self.timeout) as response: payload=json.loads(response.read().decode())
        except (URLError,TimeoutError,OSError,json.JSONDecodeError) as error:
            raise LLMUnavailableError(f"Ollama unavailable or invalid response: {error}") from error
        message=payload.get("message")
        if not isinstance(message,dict): raise LLMUnavailableError("Ollama response omitted message")
        calls=[]
        for raw in message.get("tool_calls",[]) or []:
            function=raw.get("function",raw)
            if not isinstance(function,dict) or not isinstance(function.get("name"),str) or not isinstance(function.get("arguments",{}),dict):
                raise LLMUnavailableError("Ollama returned malformed tool call")
            calls.append(ToolCall(function["name"],function.get("arguments",{}),str(raw.get("id",""))))
        return LLMResponse(str(message.get("content","")),calls,str(payload.get("model",self.model)),{"prompt_eval_count":payload.get("prompt_eval_count"),"eval_count":payload.get("eval_count")})
