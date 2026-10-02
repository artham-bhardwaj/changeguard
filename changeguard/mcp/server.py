import argparse, asyncio, os
from dataclasses import asdict
from typing import Any
from mcp.server.mcpserver import MCPServer
from changeguard.agent.agent import LocalTools
from changeguard.analysis.models import ChangeGuardAnalysis
from changeguard.analysis.service import ChangeAnalysisService
from changeguard.evidence.retrieval import TfidfLexicalRetriever
from changeguard.evidence.service import EvidenceService
from changeguard.storage.sqlite import SQLiteAnalysisStore
from changeguard.tools.filesystem_tools import get_file
from changeguard.tools.git_tools import get_git_diff, get_java_sources, get_recent_commits, get_repo_status, get_revision_file, get_revision_files
from evaluation.runner import EvaluationRunner
from changeguard.github import GitHubAPIAdapter, PullRequestAnalysisService, PullRequestRef

def create_server(database: str="changeguard.sqlite3")->MCPServer:
    store=SQLiteAnalysisStore(database)
    evidence=EvidenceService(TfidfLexicalRetriever(store))
    server=MCPServer(name="changeguard",title="ChangeGuard",description="Read-only local Git and evidence investigation tools.",version="0.3.0")
    @server.tool(name="get_repo_status",structured_output=True)
    def repo_status(repo_path: str)->dict[str,Any]: return asdict(get_repo_status(repo_path))
    @server.tool(name="get_git_diff",structured_output=True)
    def git_diff(repo_path: str,base: str,head: str)->dict[str,Any]: return asdict(get_git_diff(repo_path,base,head))
    @server.tool(name="get_file",structured_output=True)
    def file_contents(repo_path: str,path: str)->dict[str,str]: return {"content":get_file(repo_path,path)}
    @server.tool(name="get_recent_commits",structured_output=True)
    def recent_commits(repo_path: str,limit: int=10,revision: str|None=None)->dict[str,list[dict[str,str]]]: return {"commits":[asdict(item) for item in get_recent_commits(repo_path,limit,revision)]}
    @server.tool(name="get_java_sources",structured_output=True)
    def java_sources(repo_path: str,revision: str)->dict[str,Any]: return asdict(get_java_sources(repo_path,revision))
    @server.tool(name="get_revision_files",structured_output=True)
    def revision_files(repo_path: str,revision: str,max_files: int=10_000)->dict[str,Any]: return {"files":get_revision_files(repo_path,revision,max_files=max_files)}
    @server.tool(name="get_revision_file",structured_output=True)
    def revision_file(repo_path: str,revision: str,path: str,max_bytes: int=512*1024)->dict[str,str]: return {"content":get_revision_file(repo_path,revision,path,max_bytes=max_bytes)}
    @server.tool(name="analyze_change",structured_output=True)
    def analyze_change(repo_path: str,base: str,head: str)->dict[str,Any]:
        service=ChangeAnalysisService(store,LocalTools(store))
        return service.analyze(repo_path=repo_path,base=base,head=head).to_dict()
    @server.tool(name="plan_verification",structured_output=True)
    def plan_verification(analysis_id: str)->dict[str,Any]:
        value=store.get_change_analysis(analysis_id)
        if value is None: raise ValueError(f"Unknown analysis ID: {analysis_id}")
        service=ChangeAnalysisService(store,LocalTools(store))
        return asdict(service.plan_verification(ChangeGuardAnalysis.from_dict(value)))
    @server.tool(name="run_verification",structured_output=True)
    def run_verification(analysis_id: str)->dict[str,Any]:
        value=store.get_change_analysis(analysis_id)
        if value is None: raise ValueError(f"Unknown analysis ID: {analysis_id}")
        service=ChangeAnalysisService(store,LocalTools(store))
        return service.verify(ChangeGuardAnalysis.from_dict(value)).to_dict()
    @server.tool(name="get_verification_result",structured_output=True)
    def get_verification_result(analysis_id: str)->dict[str,Any]:
        value=store.get_change_analysis(analysis_id)
        if value is None: raise ValueError(f"Unknown analysis ID: {analysis_id}")
        return {
            "analysis_id": analysis_id,
            "verification_plan": value.get("verification_plan"),
            "verification_result": value.get("verification_result"),
            "initial_risk_assessment": value.get("initial_risk_assessment"),
            "risk_assessment": value.get("risk_assessment"),
        }
    @server.tool(name="get_api_impact",structured_output=True)
    def get_api_impact(analysis_id: str)->dict[str,Any]:
        value=store.get_change_analysis(analysis_id)
        if value is None: raise ValueError(f"Unknown analysis ID: {analysis_id}")
        api_impact=value.get("api_impact")
        if api_impact is None:
            return {
                "analysis_id": analysis_id,
                "status": "UNAVAILABLE",
                "reason": "This analysis has no API impact result.",
            }
        return {"analysis_id": analysis_id,"status":"AVAILABLE","api_impact":api_impact}
    @server.tool(name="search_evidence",structured_output=True)
    def search_evidence(query: str,top_k: int=5,source_type: str|None=None,repository: str|None=None)->dict[str,Any]: return asdict(evidence.search(query,top_k,source_type,repository))
    @server.tool(name="evaluate_case",structured_output=True)
    def evaluate_case(case_id: str,verify: bool=False,llm: bool=False)->dict[str,Any]:
        return EvaluationRunner(store).run_case(case_id,verify=verify,llm=llm).to_dict()
    @server.tool(name="evaluate_all",structured_output=True)
    def evaluate_all(case_ids: list[str]|None=None,verify: bool=False,llm: bool=False)->dict[str,Any]:
        selected=None if case_ids is None else tuple(case_ids)
        return EvaluationRunner(store).run_all(case_ids=selected,verify=verify,llm=llm).to_dict()
    @server.tool(name="list_evaluation_cases",structured_output=True)
    def list_evaluation_cases()->dict[str,Any]:
        return {"cases":[case.to_dict() for case in EvaluationRunner().cases()]}
    @server.tool(name="get_evaluation_result",structured_output=True)
    def get_evaluation_result(evaluation_run_id: str)->dict[str,Any]:
        result=store.get_evaluation_result(evaluation_run_id)
        if result is None: raise ValueError(f"Unknown evaluation run ID: {evaluation_run_id}")
        return result
    @server.tool(name="get_evaluation_run",structured_output=True)
    def get_evaluation_run(evaluation_run_id: str)->dict[str,Any]:
        result=store.get_evaluation_run(evaluation_run_id)
        if result is None: raise ValueError(f"Unknown evaluation run ID: {evaluation_run_id}")
        return result
    @server.tool(name="get_evaluation_summary",structured_output=True)
    def get_evaluation_summary(evaluation_run_id: str)->dict[str,Any]:
        report=store.get_evaluation_report(evaluation_run_id)
        if report is None: raise ValueError(f"Unknown evaluation summary ID: {evaluation_run_id}")
        return report
    @server.tool(name="list_evaluation_runs",structured_output=True)
    def list_evaluation_runs(limit: int=50)->dict[str,Any]:
        return {"runs":store.list_evaluation_runs(limit)}
    @server.tool(name="analyze_github_pull_request",structured_output=True)
    def analyze_github_pull_request(
        owner: str,
        repository: str,
        pull_number: int,
        verify: bool=False,
        llm: bool=False,
        persist: bool=False,
        refresh_head: bool=True,
    )->dict[str,Any]:
        token=os.environ.get("GITHUB_TOKEN")
        reference=PullRequestRef(owner,repository,pull_number)
        return PullRequestAnalysisService(
            GitHubAPIAdapter(token=token),store,token=token
        ).analyze(
            reference,verify=verify,llm=llm,persist=persist,
            refresh_head=refresh_head,
        ).to_dict()
    return server
def main()->None:
    parser=argparse.ArgumentParser(); parser.add_argument("--database",default="changeguard.sqlite3"); args=parser.parse_args()
    asyncio.run(create_server(args.database).run_stdio_async())
if __name__=="__main__": main()
