from __future__ import annotations
import math, re
from collections import Counter
from typing import Protocol
from .models import RetrievedEvidence
class EvidenceReadStore(Protocol):
    def list_evidence_chunks(self, source_type: str|None=None, repository: str|None=None) -> list[dict[str, object]]: ...
def tokens(text: str) -> list[str]: return re.findall(r"[a-z0-9_]{2,}", text.lower())
class LexicalRetriever(Protocol):
    def retrieve(self, query: str, top_k: int=5, source_type: str|None=None, repository: str|None=None) -> list[RetrievedEvidence]: ...
class TfidfLexicalRetriever:
    method="tfidf_cosine_v1"
    def __init__(self, store: EvidenceReadStore) -> None: self.store=store
    def retrieve(self, query: str, top_k: int=5, source_type: str|None=None, repository: str|None=None) -> list[RetrievedEvidence]:
        if top_k < 1: raise ValueError("top_k must be positive")
        rows=self.store.list_evidence_chunks(source_type,repository)
        query_tokens=tokens(query)
        if not rows or not query_tokens: return []
        documents=[tokens(str(row["content"])) for row in rows]
        vocab=set(query_tokens)
        df={term:sum(term in set(document) for document in documents) for term in vocab}
        n=len(documents)
        def vector(words: list[str]) -> dict[str,float]:
            counts=Counter(words); length=max(1,len(words))
            return {term:(counts[term]/length)*(math.log((n+1)/(df[term]+1))+1) for term in vocab}
        query_vector=vector(query_tokens); qnorm=math.sqrt(sum(x*x for x in query_vector.values()))
        scored=[]
        for row, words in zip(rows,documents):
            doc_vector=vector(words); dnorm=math.sqrt(sum(x*x for x in doc_vector.values()))
            score=0.0 if not dnorm or not qnorm else sum(query_vector[t]*doc_vector[t] for t in vocab)/(qnorm*dnorm)
            if score>0: scored.append(RetrievedEvidence(str(row["evidence_id"]),str(row["chunk_id"]),str(row["title"]),str(row["source"]),score,str(row["content"]),dict(row["metadata"]),str(row.get("source_type","unknown"))))
        return sorted(scored,key=lambda result:(-result.similarity_score,result.evidence_id,result.chunk_id))[:top_k]
class VectorRetriever(Protocol): pass
class HybridRetriever(Protocol): pass
