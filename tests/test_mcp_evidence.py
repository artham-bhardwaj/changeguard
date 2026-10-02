import sys
from changeguard.evidence.ingest import EvidenceIngestor
from changeguard.evidence.models import EvidenceDocument
from changeguard.mcp import MCPToolPort
from changeguard.storage.sqlite import SQLiteAnalysisStore
def test_search_evidence_over_mcp(tmp_path):
    database=str(tmp_path/"evidence.db"); store=SQLiteAnalysisStore(database)
    EvidenceIngestor(store).ingest(EvidenceDocument("Incident","incident","INC-1","repo","database timeout payment",evidence_id="INC-1"))
    port=MCPToolPort([sys.executable,"-m","changeguard.mcp.server","--database",database])
    result=port.search_evidence("database timeout",5,repository="repo")
    assert result.status=="ok" and result.results[0].evidence_id=="INC-1"
