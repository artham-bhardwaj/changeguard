from __future__ import annotations
from .models import EvidenceBundle
from .retrieval import TfidfLexicalRetriever
from .models import now
class EvidenceService:
    def __init__(self, retriever: TfidfLexicalRetriever) -> None: self.retriever=retriever
    def search(self, query: str, top_k: int=5, source_type: str|None=None, repository: str|None=None) -> EvidenceBundle:
        results=self.retriever.retrieve(query,top_k,source_type,repository)
        return EvidenceBundle(query,results,now(),self.retriever.method,top_k,{"source_type":source_type,"repository":repository},"ok" if results else "no_relevant_evidence")
