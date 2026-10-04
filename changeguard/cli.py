from __future__ import annotations
import argparse,json,os,sys
from changeguard.agent import DeterministicAgent
from changeguard.analysis import ChangeAnalysisService
from changeguard.llm import GeminiLLMAdapter, OllamaLLMAdapter, OllamaLLMClient, load_local_env
from changeguard.llm.agent import BoundedInvestigationAgent
from changeguard.llm.agent import ToolUsingAgent
from changeguard.mcp import MCPToolPort
from changeguard.storage import SQLiteAnalysisStore
from changeguard.github import GitHubAPIAdapter, PullRequestAnalysisService, PullRequestRef
def main()->None:
    load_local_env()
    parser=argparse.ArgumentParser(); sub=parser.add_subparsers(dest="command"); analyze=sub.add_parser("analyze")
    analyze.add_argument("--repo",required=True); analyze.add_argument("--base",required=True); analyze.add_argument("--head",required=True); analyze.add_argument("--agent",choices=["llm","deterministic"],default="deterministic"); analyze.add_argument("--database",default="changeguard.sqlite3")
    impact=sub.add_parser("impact",help="run end-to-end deterministic change impact analysis")
    impact.add_argument("--repo",required=True); impact.add_argument("--base",required=True); impact.add_argument("--head",required=True)
    impact.add_argument("--database",default="changeguard.sqlite3"); impact.add_argument("--llm",action="store_true")
    impact.add_argument("--llm-provider",choices=["ollama","gemini"],default="ollama")
    impact.add_argument("--max-tool-calls",type=int,default=3)
    impact.add_argument("--verify",action="store_true",help="explicitly execute planned verification in an isolated sandbox")
    evaluate=sub.add_parser("evaluate",help="run authored local evaluation fixtures")
    evaluate_group=evaluate.add_mutually_exclusive_group()
    evaluate_group.add_argument("--case",help="run one registered evaluation case")
    evaluate_group.add_argument("--all",action="store_true",help="run all registered evaluation cases")
    evaluate_group.add_argument("--github",action="store_true",help="run offline mocked GitHub PR workflow scenarios")
    evaluate.add_argument("--verify",action="store_true",help="explicitly enable sandboxed verification")
    evaluate.add_argument("--llm",action="store_true",help="opt in to Ollama-backed agent evaluation")
    evaluate.add_argument("--database",default="changeguard.sqlite3")
    pr=sub.add_parser("pr",help="analyze a GitHub pull request at immutable commit revisions")
    pr_sub=pr.add_subparsers(dest="pr_command")
    pr_analyze=pr_sub.add_parser("analyze",help="analyze OWNER/REPO#NUMBER")
    pr_analyze.add_argument("pull_request")
    pr_analyze.add_argument("--database",default="changeguard.sqlite3")
    pr_analyze.add_argument("--verify",action="store_true",help="opt in to safe sandboxed verification")
    pr_analyze.add_argument("--llm",action="store_true",help="opt in to local Ollama investigation")
    pr_analyze.add_argument("--persist",action="store_true",help="persist/cache completed exact-revision results")
    pr_analyze.add_argument("--json",action="store_true",help="emit the structured result as JSON")
    pr_analyze.add_argument("--refresh-head",action=argparse.BooleanOptionalAction,default=True,
                            help="check the PR head again after analysis (default: enabled)")
    args=parser.parse_args()
    if args.command=="pr":
        if args.pr_command!="analyze":
            parser.error("use: changeguard pr analyze OWNER/REPO#NUMBER")
        try:
            reference=PullRequestRef.parse(args.pull_request)
        except ValueError as error:
            parser.error(str(error))
        token=os.environ.get("GITHUB_TOKEN")
        store=SQLiteAnalysisStore(args.database)
        result=PullRequestAnalysisService(
            GitHubAPIAdapter(token=token),store,token=token
        ).analyze(
            reference,verify=args.verify,llm=args.llm,persist=args.persist,
            refresh_head=args.refresh_head,
        )
        if args.json:
            print(json.dumps(result.to_dict(),indent=2))
        else:
            print(f"{result.status}: {result.repository}#{result.pull_number}")
            if result.head_sha:
                print(f"Analyzed head: {result.head_sha}")
            if result.status=="STALE":
                print(f"Current head: {result.current_head_sha}")
            if result.error:
                print(f"{result.error.code}: {result.error.message}")
            elif result.analysis:
                print(result.analysis["change_summary"])
                print(f"Changed files: {len(result.changed_files)}")
                api_impact=result.analysis.get("api_impact")
                if isinstance(api_impact,dict):
                    print(
                        "API impact: "
                        f"{len(api_impact.get('endpoints',[]))} endpoint(s), "
                        f"{len(api_impact.get('edges',[]))} deterministic consumer edge(s), "
                        f"{len(api_impact.get('ambiguous_relationships',[]))} ambiguous, "
                        f"{len(api_impact.get('unresolved_relationships',[]))} unresolved"
                    )
        if result.status=="FAILED":
            raise SystemExit(2)
        return
    if args.command=="evaluate":
        from evaluation.runner import EvaluationRunner
        if args.github:
            if args.verify or args.llm:
                parser.error("--verify and --llm are not options for offline GitHub workflow evaluation")
            print(json.dumps(EvaluationRunner().run_github_workflows(),indent=2))
            return
        store=SQLiteAnalysisStore(args.database)
        runner=EvaluationRunner(store)
        output=(
            runner.run_case(args.case,verify=args.verify,llm=args.llm).to_dict()
            if args.case
            else runner.run_all(verify=args.verify,llm=args.llm).to_dict()
        )
        print(json.dumps(output,indent=2))
        return
    if args.command=="impact":
        store=SQLiteAnalysisStore(args.database)
        mcp=MCPToolPort(evidence_database=args.database)
        investigator=(
            BoundedInvestigationAgent(
                GeminiLLMAdapter() if args.llm_provider=="gemini" else OllamaLLMAdapter(),mcp,max_tool_calls=min(args.max_tool_calls,1) if args.llm_provider=="gemini" else args.max_tool_calls
            )
            if args.llm else None
        )
        service=ChangeAnalysisService(store,mcp,investigator=investigator)
        result=service.analyze(
            repo_path=args.repo,base=args.base,head=args.head
        )
        if args.verify:
            result=service.verify(result)
        print(json.dumps({"mode":"llm_enhanced" if args.llm else "deterministic","analysis":result.to_dict()},indent=2))
        return
    if args.command!="analyze": parser.error("use: changeguard analyze")
    store=SQLiteAnalysisStore(args.database)
    if args.agent=="deterministic":
        state=DeterministicAgent(store).investigate(repo_path=args.repo,base=args.base,head=args.head); print(json.dumps({"mode":"deterministic","analysis":state.to_dict()},indent=2)); return
    result=ToolUsingAgent(store,OllamaLLMClient(),MCPToolPort(evidence_database=args.database)).investigate(args.repo,args.base,args.head)
    print(json.dumps({"mode":result.mode,"report":result.report,"grounding":result.grounding,"total_steps":result.total_steps},indent=2))
if __name__=="__main__": main()
