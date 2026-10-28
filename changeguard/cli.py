from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import get_args

from changeguard.agent import DeterministicAgent
from changeguard.analysis import ChangeAnalysisService
from changeguard.github import GitHubAPIAdapter, PullRequestAnalysisService, PullRequestRef
from changeguard.llm import (
    LLM_PROVIDERS,
    OllamaError,
    OllamaLLMClient,
    create_llm_adapter,
    default_llm_provider,
    load_local_env,
    max_tool_calls_for,
)
from changeguard.llm.agent import BoundedInvestigationAgent, ToolUsingAgent
from changeguard.mcp import MCPToolPort
from changeguard.report import render_markdown
from changeguard.settings import default_database_path, github_token, repository_key
from changeguard.storage import SQLiteAnalysisStore


def main() -> None:
    load_local_env()
    parser = _build_parser()
    args = parser.parse_args()
    handlers = {
        "demo": _run_demo,
        "pr": _run_pr,
        "impact": _run_impact,
        "evidence": _run_evidence,
        "evaluate": _run_evaluate,
        "analyze": _run_legacy_analyze,
    }
    handler = handlers.get(args.command)
    if handler is None:
        parser.print_help()
        raise SystemExit(2)
    handler(args, parser)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="changeguard",
        description="Evidence-backed change impact analysis for pull requests and commit ranges.",
    )
    try:
        provider_default = default_llm_provider()
    except ValueError as error:
        parser.error(str(error))
    provider_help = (
        f"LLM used with --llm (default: {provider_default}; set CHANGEGUARD_LLM_PROVIDER to change)"
    )
    database_help = f"SQLite database for analyses and evidence (default: {default_database_path()})"
    sub = parser.add_subparsers(dest="command")

    pr = sub.add_parser("pr", help="analyze a GitHub pull request")
    pr_sub = pr.add_subparsers(dest="pr_command")
    pr_analyze = pr_sub.add_parser("analyze", help="analyze OWNER/REPO#NUMBER (or a PR URL)")
    pr_analyze.add_argument("pull_request", help="OWNER/REPO#NUMBER or https://github.com/OWNER/REPO/pull/NUMBER")
    pr_analyze.add_argument("--database", default=default_database_path(), help=database_help)
    pr_analyze.add_argument("--verify", action="store_true", help="run planned checks in a bubblewrap sandbox")
    pr_analyze.add_argument("--llm", action="store_true", help="add a bounded AI investigation")
    pr_analyze.add_argument("--llm-provider", choices=LLM_PROVIDERS, default=provider_default, help=provider_help)
    pr_analyze.add_argument("--persist", action="store_true", help="cache completed exact-revision results")
    pr_analyze.add_argument("--json", action="store_true", help="output the full structured result as JSON")
    pr_analyze.add_argument("--output", "-o", help="also write the report (or JSON with --json) to this file")
    pr_analyze.add_argument(
        "--refresh-head", action=argparse.BooleanOptionalAction, default=True,
        help="check the PR head again after analysis (default: enabled)",
    )

    impact = sub.add_parser("impact", help="analyze a commit range in a local git repository")
    impact.add_argument("--repo", default=".", help="repository path (default: current directory)")
    impact.add_argument("--base", default="HEAD~1", help="base revision (default: HEAD~1)")
    impact.add_argument("--head", default="HEAD", help="head revision (default: HEAD)")
    impact.add_argument("--database", default=default_database_path(), help=database_help)
    impact.add_argument("--llm", action="store_true", help="add a bounded AI investigation")
    impact.add_argument("--llm-provider", choices=LLM_PROVIDERS, default=provider_default, help=provider_help)
    impact.add_argument("--max-tool-calls", type=int, default=3, help="AI tool-call budget (Gemini is capped at 1)")
    impact.add_argument("--verify", action="store_true", help="run planned checks in a bubblewrap sandbox")
    impact.add_argument("--json", action="store_true", help="output the full structured result as JSON")
    impact.add_argument("--output", "-o", help="also write the report (or JSON with --json) to this file")

    evidence = sub.add_parser("evidence", help="manage historical evidence used during analysis")
    evidence_sub = evidence.add_subparsers(dest="evidence_command")
    source_types = get_args(_source_type())
    evidence_add = evidence_sub.add_parser("add", help="add text/Markdown files as evidence")
    evidence_add.add_argument("files", nargs="+", help="files to add (e.g. incident write-ups, ADRs)")
    evidence_add.add_argument("--type", required=True, choices=source_types, dest="source_type")
    evidence_add.add_argument("--repo", help="OWNER/REPO the evidence belongs to (default: current git repo)")
    evidence_add.add_argument("--title", help="title (default: first Markdown heading or file name)")
    evidence_add.add_argument("--database", default=default_database_path(), help=database_help)
    evidence_list = evidence_sub.add_parser("list", help="list stored evidence")
    evidence_list.add_argument("--repo", help="only evidence for OWNER/REPO")
    evidence_list.add_argument("--database", default=default_database_path(), help=database_help)
    evidence_search = evidence_sub.add_parser("search", help="search stored evidence")
    evidence_search.add_argument("query")
    evidence_search.add_argument("--repo", help="only evidence for OWNER/REPO")
    evidence_search.add_argument("--type", choices=source_types, dest="source_type")
    evidence_search.add_argument("--top-k", type=int, default=5)
    evidence_search.add_argument("--database", default=default_database_path(), help=database_help)

    sub.add_parser("demo", help="start the local browser demo (http://127.0.0.1:8000)")

    evaluate = sub.add_parser("evaluate", help="run authored local evaluation fixtures")
    evaluate_group = evaluate.add_mutually_exclusive_group()
    evaluate_group.add_argument("--case", help="run one registered evaluation case")
    evaluate_group.add_argument("--all", action="store_true", help="run all registered evaluation cases")
    evaluate_group.add_argument("--github", action="store_true", help="run offline mocked GitHub PR workflow scenarios")
    evaluate.add_argument("--verify", action="store_true", help="explicitly enable sandboxed verification")
    evaluate.add_argument("--llm", action="store_true", help="opt in to Ollama-backed agent evaluation")
    evaluate.add_argument("--database", default="changeguard.sqlite3")

    analyze = sub.add_parser("analyze", help="legacy agent analysis (prefer 'impact')")
    analyze.add_argument("--repo", required=True)
    analyze.add_argument("--base", required=True)
    analyze.add_argument("--head", required=True)
    analyze.add_argument("--agent", choices=["llm", "deterministic"], default="deterministic")
    analyze.add_argument("--database", default="changeguard.sqlite3")
    return parser


def _source_type():
    from changeguard.evidence.models import SourceType
    return SourceType


_PR_URL = re.compile(r"^https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)(?:[/?#].*)?$")


def _parse_pull_request(value: str) -> PullRequestRef:
    match = _PR_URL.match(value.strip())
    if match:
        value = f"{match[1]}/{match[2]}#{match[3]}"
    return PullRequestRef.parse(value)


def _run_demo(_args, _parser) -> None:
    from changeguard.demo_server import main as demo_main
    demo_main()


def _run_pr(args, parser) -> None:
    if args.pr_command != "analyze":
        parser.error("use: changeguard pr analyze OWNER/REPO#NUMBER")
    try:
        reference = _parse_pull_request(args.pull_request)
    except ValueError as error:
        parser.error(str(error))
    token = github_token()
    store = SQLiteAnalysisStore(args.database)
    result = PullRequestAnalysisService(
        GitHubAPIAdapter(token=token), store, token=token, llm_provider=args.llm_provider
    ).analyze(
        reference, verify=args.verify, llm=args.llm, persist=args.persist,
        refresh_head=args.refresh_head,
    )
    payload = result.to_dict()
    if args.json:
        _emit(json.dumps(payload, indent=2), args.output)
    elif result.analysis:
        _emit(render_markdown(result.analysis, pr=payload), args.output)
    else:
        lines = [f"{result.status}: {result.repository}#{result.pull_number}"]
        if result.error:
            lines.append(f"{result.error.code}: {result.error.message}")
            if result.error.code in {"NOT_FOUND", "ACCESS_DENIED", "AUTHENTICATION_FAILED", "RATE_LIMITED"} and not token:
                lines.append("Hint: for private repositories set GITHUB_TOKEN or run `gh auth login`.")
        _emit("\n".join(lines), args.output)
    if result.status == "FAILED":
        raise SystemExit(2)


def _run_impact(args, parser) -> None:
    repo = str(Path(args.repo).resolve())
    base = _resolve_commit(parser, repo, args.base)
    head = _resolve_commit(parser, repo, args.head)
    store = SQLiteAnalysisStore(args.database)
    mcp = MCPToolPort(evidence_database=args.database)
    investigator = None
    if args.llm:
        try:
            adapter = create_llm_adapter(args.llm_provider)
        except (OllamaError, ValueError) as error:
            parser.error(str(error))
        investigator = BoundedInvestigationAgent(
            adapter, mcp, max_tool_calls=max_tool_calls_for(args.llm_provider, args.max_tool_calls)
        )
    service = ChangeAnalysisService(store, mcp, investigator=investigator)
    result = service.analyze(
        repo_path=repo, base=base, head=head, evidence_repository=repository_key(repo)
    )
    if args.verify:
        result = service.verify(result)
    analysis = result.to_dict()
    if args.json:
        output = json.dumps(
            {"mode": "llm_enhanced" if args.llm else "deterministic", "analysis": analysis}, indent=2
        )
    else:
        output = render_markdown(analysis)
    _emit(output, args.output)


def _run_evidence(args, parser) -> None:
    from changeguard.evidence.ingest import EvidenceIngestor
    from changeguard.evidence.models import EvidenceDocument
    from changeguard.evidence.retrieval import TfidfLexicalRetriever
    from changeguard.evidence.service import EvidenceService

    store = SQLiteAnalysisStore(args.database)
    if args.evidence_command == "add":
        repository = (args.repo or repository_key(".")).lower()
        ingestor = EvidenceIngestor(store)
        for name in args.files:
            path = Path(name)
            try:
                content = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as error:
                parser.error(f"cannot read {name}: {error}")
            if not content.strip():
                print(f"skipped {name}: empty file")
                continue
            title = args.title or _markdown_title(content) or path.stem.replace("-", " ").replace("_", " ")
            document, duplicate = ingestor.ingest(
                EvidenceDocument(title, args.source_type, str(path.resolve()), repository, content)
            )
            state = "already stored" if duplicate else "added"
            print(f"{state}: [{args.source_type}] {title} ({repository})")
        return
    if args.evidence_command == "list":
        repository = args.repo.lower() if args.repo else None
        seen: dict[str, dict[str, object]] = {}
        for row in store.list_evidence_chunks(None, repository):
            seen.setdefault(str(row["evidence_id"]), row)
        if not seen:
            print("No evidence stored. Add some with: changeguard evidence add FILE --type incident")
        for row in seen.values():
            print(f"[{row['source_type']}] {row['title']}  ({row['source']})")
        return
    if args.evidence_command == "search":
        repository = args.repo.lower() if args.repo else None
        bundle = EvidenceService(TfidfLexicalRetriever(store)).search(
            args.query, args.top_k, args.source_type, repository
        )
        if not bundle.results:
            print("No matching evidence.")
        for item in bundle.results:
            snippet = " ".join(item.matched_text.split())[:160]
            print(f"{item.similarity_score:.2f}  [{item.source_type}] {item.title}\n      {snippet}")
        return
    parser.error("use: changeguard evidence {add,list,search}")


def _run_evaluate(args, parser) -> None:
    from evaluation.runner import EvaluationRunner

    if args.github:
        if args.verify or args.llm:
            parser.error("--verify and --llm are not options for offline GitHub workflow evaluation")
        print(json.dumps(EvaluationRunner().run_github_workflows(), indent=2))
        return
    runner = EvaluationRunner(SQLiteAnalysisStore(args.database))
    output = (
        runner.run_case(args.case, verify=args.verify, llm=args.llm).to_dict()
        if args.case
        else runner.run_all(verify=args.verify, llm=args.llm).to_dict()
    )
    print(json.dumps(output, indent=2))


def _run_legacy_analyze(args, _parser) -> None:
    store = SQLiteAnalysisStore(args.database)
    if args.agent == "deterministic":
        state = DeterministicAgent(store).investigate(repo_path=args.repo, base=args.base, head=args.head)
        print(json.dumps({"mode": "deterministic", "analysis": state.to_dict()}, indent=2))
        return
    result = ToolUsingAgent(store, OllamaLLMClient(), MCPToolPort(evidence_database=args.database)).investigate(
        args.repo, args.base, args.head
    )
    print(json.dumps({
        "mode": result.mode, "report": result.report,
        "grounding": result.grounding, "total_steps": result.total_steps,
    }, indent=2))


def _resolve_commit(parser: argparse.ArgumentParser, repo: str, revision: str) -> str:
    """Resolve a revision such as HEAD~1 or a branch name to a full commit SHA."""
    try:
        completed = subprocess.run(
            ["git", "-C", repo, "rev-parse", "--verify", "--quiet", "--end-of-options", f"{revision}^{{commit}}"],
            capture_output=True, text=True, timeout=30, check=False,
        )
    except (OSError, subprocess.SubprocessError) as error:
        parser.error(f"cannot run git: {error}")
    if completed.returncode != 0:
        parser.error(f"{revision!r} is not a commit in {repo}")
    return completed.stdout.strip()


def _markdown_title(content: str) -> str | None:
    for line in content.splitlines():
        if line.startswith("# "):
            return line[2:].strip() or None
    return None


def _emit(text: str, output: str | None) -> None:
    if not text.endswith("\n"):
        text += "\n"
    sys.stdout.write(text)
    if output:
        Path(output).write_text(text, encoding="utf-8")
        print(f"Saved to {output}", file=sys.stderr)


if __name__ == "__main__":
    main()
