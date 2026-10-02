from __future__ import annotations
from typing import Protocol
from changeguard.evidence.query import build_change_query
from changeguard.evidence.retrieval import TfidfLexicalRetriever
from changeguard.evidence.service import EvidenceService
from changeguard.models.schemas import AnalysisState, AgentTrace, CommitMetadata, DiffResult, Evidence, JavaSourceSnapshot, RepoStatus
from changeguard.storage.sqlite import SQLiteAnalysisStore
from changeguard.tools.git_tools import get_git_diff, get_java_sources, get_recent_commits, get_repo_status, get_revision_file, get_revision_files

class ToolPort(Protocol):
    transport: str
    def repo_status(self,repo_path: str)->RepoStatus: ...
    def git_diff(self,repo_path: str,base: str,head: str)->DiffResult: ...
    def recent_commits(self,repo_path: str,limit: int,revision: str|None=None)->list[CommitMetadata]: ...
    def search_evidence(self,query: str,top_k: int=5,source_type: str|None=None,repository: str|None=None): ...

class JavaSourcePort(Protocol):
    def java_sources(self, repo_path: str, revision: str) -> JavaSourceSnapshot: ...

class RevisionReadPort(Protocol):
    def revision_files(self, repo_path: str, revision: str, max_files: int = 10_000) -> tuple[str, ...]: ...
    def revision_file(self, repo_path: str, revision: str, path: str, max_bytes: int = 512 * 1024) -> str: ...

class LocalTools:
    transport="local"
    def __init__(self,store: SQLiteAnalysisStore|None=None)->None:
        self.evidence=EvidenceService(TfidfLexicalRetriever(store)) if store else None
    def repo_status(self,repo_path: str)->RepoStatus: return get_repo_status(repo_path)
    def git_diff(self,repo_path: str,base: str,head: str)->DiffResult: return get_git_diff(repo_path,base,head)
    def recent_commits(self,repo_path: str,limit: int,revision: str|None=None)->list[CommitMetadata]: return get_recent_commits(repo_path,limit,revision)
    def java_sources(self,repo_path: str,revision: str)->JavaSourceSnapshot: return get_java_sources(repo_path,revision)
    def revision_files(self,repo_path: str,revision: str,max_files: int=10_000)->tuple[str,...]: return get_revision_files(repo_path,revision,max_files=max_files)
    def revision_file(self,repo_path: str,revision: str,path: str,max_bytes: int=512*1024)->str: return get_revision_file(repo_path,revision,path,max_bytes=max_bytes)
    def search_evidence(self,query: str,top_k: int=5,source_type: str|None=None,repository: str|None=None):
        if not self.evidence: raise RuntimeError("Local evidence store is required")
        return self.evidence.search(query,top_k,source_type,repository)

class DeterministicAgent:
    max_tool_calls=4
    def __init__(self,store: SQLiteAnalysisStore,tools: ToolPort|None=None)->None:
        self.store,self.tools=store,tools or LocalTools(store)
        if isinstance(self.tools,LocalTools) and self.tools.evidence is None: self.tools.evidence=EvidenceService(TfidfLexicalRetriever(store))
    def investigate(self,*,repo_path: str,base: str,head: str)->AnalysisState:
        trace=[]
        status=self.tools.repo_status(repo_path); trace.append(AgentTrace(1,"get_repo_status",{"repo_path":repo_path},f"branch={status.branch}, modified_files={len(status.modified_files)}",self.tools.transport))
        diff=self.tools.git_diff(repo_path,base,head); trace.append(AgentTrace(2,"get_git_diff",{"repo_path":repo_path,"base":base,"head":head},f"files={len(diff.changed_files)}, +{diff.additions}/-{diff.deletions}",self.tools.transport))
        commits=self.tools.recent_commits(repo_path,10); trace.append(AgentTrace(3,"get_recent_commits",{"repo_path":repo_path,"limit":10},f"commits={len(commits)}",self.tools.transport))
        draft=AnalysisState(repo_path,base,head,diff.changed_files,{"additions":diff.additions,"deletions":diff.deletions},commits,[],trace,"in_progress")
        query=build_change_query(draft,diff.diff_text); bundle=self.tools.search_evidence(query,5,repository=repo_path)
        trace.append(AgentTrace(4,"search_evidence",{"query":query,"top_k":5,"repository":repo_path},f"status={bundle.status}, results={[item.evidence_id for item in bundle.results]}",self.tools.transport))
        evidence=[Evidence("repository_status","Repository status collected",{"branch":status.branch,"head":status.head_commit}),Evidence("git_diff","Diff collected",{"changed_files":diff.changed_files,"additions":diff.additions,"deletions":diff.deletions}),Evidence("recent_commits","Recent commits collected",{"count":len(commits)}),Evidence("evidence_retrieval",bundle.status,{"query":query,"evidence_ids":[x.evidence_id for x in bundle.results],"method":bundle.retrieval_method})]
        state=AnalysisState(repo_path,base,head,diff.changed_files,{"additions":diff.additions,"deletions":diff.deletions},commits,evidence,trace,"completed")
        self.store.save_analysis(state); return state
