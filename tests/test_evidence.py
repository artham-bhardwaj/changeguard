from __future__ import annotations
from changeguard.evidence.ingest import EvidenceIngestor, chunk_content, content_hash
from changeguard.evidence.models import EvidenceDocument
from changeguard.evidence.query import build_change_query
from changeguard.evidence.retrieval import TfidfLexicalRetriever
from changeguard.evidence.service import EvidenceService
from changeguard.models.schemas import AnalysisState
from changeguard.storage.sqlite import SQLiteAnalysisStore

def document(evidence_id="INC-001"):
    return EvidenceDocument("Pool timeout incident","incident","INC-001","payments","Database connection pool timeout exhausted during checkout payment.",{"severity":"high"},evidence_id=evidence_id)
def test_ingestion_hash_duplicate_and_chunks(tmp_path):
    store=SQLiteAnalysisStore(str(tmp_path/"evidence.db")); ingestor=EvidenceIngestor(store,chunk_size=20,overlap=5)
    saved,duplicate=ingestor.ingest(document())
    again,duplicate_again=ingestor.ingest(document("INC-999"))
    assert not duplicate and duplicate_again and saved.content_hash==content_hash(saved.content)
    assert again.content_hash==saved.content_hash and len(store.list_evidence_chunks())>1
    assert [x.chunk_index for x in chunk_content("x","abcdef",4,1)]==[0,1]
def test_tfidf_order_filters_provenance_and_bundle(tmp_path):
    store=SQLiteAnalysisStore(str(tmp_path/"evidence.db")); ingestor=EvidenceIngestor(store)
    ingestor.ingest(document())
    ingestor.ingest(EvidenceDocument("Release","release_note","REL-1","other","Frontend color adjustment.",evidence_id="REL-1"))
    service=EvidenceService(TfidfLexicalRetriever(store))
    bundle=service.search("database pool timeout",5,repository="payments")
    assert bundle.status=="ok" and bundle.results[0].evidence_id=="INC-001"
    assert bundle.results[0].source=="INC-001" and bundle.results[0].metadata["severity"]=="high"
    assert service.search("unmatched token",5).status=="no_relevant_evidence"
def test_change_query_is_deterministic():
    state=AnalysisState("/repos/payments","a","b",["src/payment/DatabasePool.java"],{"additions":1,"deletions":0},[],[],[],"completed")
    assert build_change_query(state,"timeout retry payment") == build_change_query(state,"timeout retry payment")
