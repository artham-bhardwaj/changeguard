from __future__ import annotations
import json
from pathlib import Path
from changeguard.evidence.ingest import EvidenceIngestor
from changeguard.evidence.models import EvidenceDocument
from changeguard.evidence.retrieval import TfidfLexicalRetriever
from changeguard.evidence.service import EvidenceService
from changeguard.storage.sqlite import SQLiteAnalysisStore
from evaluation.metrics import mrr, recall_at_k
def seed(store: SQLiteAnalysisStore)->None:
    ingestor=EvidenceIngestor(store)
    ingestor.ingest(EvidenceDocument("Database timeout incident","incident","INC-001","payments","Database connection pool timeout exhausted while payment retries increased.",evidence_id="INC-001"))
    ingestor.ingest(EvidenceDocument("Payment retry incident","incident","INC-002","payments","Payment downstream failure requires bounded retry and circuit breaker.",evidence_id="INC-002"))
def run(database: str="evaluation.sqlite3")->dict[str,float]:
    store=SQLiteAnalysisStore(database); seed(store); service=EvidenceService(TfidfLexicalRetriever(store))
    rows=json.loads((Path(__file__).parent/"dataset.json").read_text())
    bundles=[(service.search(row["query"],5),row["relevant_evidence"]) for row in rows]
    return {"Recall@1":sum(recall_at_k(x,y,1) for x,y in bundles)/len(bundles),"Recall@3":sum(recall_at_k(x,y,3) for x,y in bundles)/len(bundles),"Recall@5":sum(recall_at_k(x,y,5) for x,y in bundles)/len(bundles),"MRR":sum(mrr(x,y) for x,y in bundles)/len(bundles)}
if __name__=="__main__": print(json.dumps(run(),indent=2))
