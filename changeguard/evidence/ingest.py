from __future__ import annotations
import hashlib
import re
from dataclasses import replace
from typing import Protocol
from .models import EvidenceChunk, EvidenceDocument
class EvidenceWriteStore(Protocol):
    def find_evidence_by_hash(self, content_hash: str) -> str | None: ...
    def save_evidence_document(self, document: EvidenceDocument, chunks: list[EvidenceChunk]) -> None: ...
def normalize(content: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", content.replace("\r\n", "\n").strip()) + "\n"
def content_hash(content: str) -> str: return hashlib.sha256(content.encode("utf-8")).hexdigest()
def chunk_content(evidence_id: str, content: str, chunk_size: int, overlap: int) -> list[EvidenceChunk]:
    if chunk_size < 1 or overlap < 0 or overlap >= chunk_size: raise ValueError("chunk_size must be positive and overlap smaller than chunk_size")
    chunks=[]
    for index, start in enumerate(range(0, len(content), chunk_size-overlap)):
        text=content[start:start+chunk_size]
        if not text: break
        chunk_id=f"{evidence_id}:{index}:{hashlib.sha256(text.encode()).hexdigest()[:12]}"
        chunks.append(EvidenceChunk(chunk_id,evidence_id,index,text,len(text)))
        if start+chunk_size >= len(content): break
    return chunks
class EvidenceIngestor:
    def __init__(self, store: EvidenceWriteStore, chunk_size: int=800, overlap: int=120) -> None:
        self.store,self.chunk_size,self.overlap=store,chunk_size,overlap
    def ingest(self, document: EvidenceDocument) -> tuple[EvidenceDocument, bool]:
        normalized=normalize(document.content)
        hashed=replace(document, content=normalized, content_hash=content_hash(normalized))
        if self.store.find_evidence_by_hash(hashed.content_hash): return hashed, True
        self.store.save_evidence_document(hashed,chunk_content(hashed.evidence_id,normalized,self.chunk_size,self.overlap))
        return hashed, False
