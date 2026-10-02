from evaluation.metrics import mrr, recall_at_k
from changeguard.evidence.models import EvidenceBundle, RetrievedEvidence
def bundle(ids):
    return EvidenceBundle("q",[RetrievedEvidence(x,x,"t","s",1.0,"text",{}) for x in ids],"now","tfidf",5,{},"ok")
def test_metrics():
    value=bundle(["x","INC-001"])
    assert recall_at_k(value,["INC-001"],1)==0
    assert recall_at_k(value,["INC-001"],3)==1
    assert mrr(value,["INC-001"])==0.5
