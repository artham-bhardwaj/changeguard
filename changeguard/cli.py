from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import asdict
from pathlib import Path
from typing import get_args

from changeguard import gate
from changeguard.agent import DeterministicAgent
from changeguard.github import PullRequestRef
from changeguard.github.comments import CommentError, post_report_comment
from changeguard.history import HistoryStore
from changeguard.llm import (
    LLM_PROVIDERS,
    OllamaError,
    OllamaLLMClient,
    default_llm_provider,
    load_local_env,
)
from changeguard.llm.agent import ToolUsingAgent
from changeguard.mcp import MCPToolPort
from changeguard.runner import analyze_local, analyze_pull_request
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
        "init": _run_init,
        "doctor": _run_doctor,
        "ui": _run_demo,
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
    fail_on_help = (
        "exit with code 4 when risk is at or above LEVEL (or the analysis is incomplete), "
        "so CI or a pre-push hook can stop the change for senior review"
    )

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
    pr_analyze.add_argument("--comment", action="store_true", help="post/update the report as a PR comment")
    pr_analyze.add_argument(
        "--refresh-head", action=argparse.BooleanOptionalAction, default=True,
        help="check the PR head again after analysis (default: enabled)",
    )
    pr_analyze.add_argument("--fail-on", type=str.upper, choices=GATE_LEVELS, metavar="LEVEL", help=fail_on_help)
    pr_analyze.add_argument(
        "--approvers",
        help="comma-separated GitHub logins whose approval of the latest commit clears the gate "
        "(default: CHANGEGUARD_APPROVERS, else any owner, member or collaborator)",
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
    impact.add_argument("--fail-on", type=str.upper, choices=GATE_LEVELS, metavar="LEVEL", help=fail_on_help)

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
    evidence_sync = evidence_sub.add_parser(
        "sync", help="import .changeguard/evidence/ and docs/{incidents,adr,runbooks,...} folders"
    )
    evidence_sync.add_argument("path", nargs="?", default=".", help="repository path (default: current directory)")
    evidence_sync.add_argument("--repo", help="OWNER/REPO to file the evidence under (default: from git remote)")
    evidence_sync.add_argument("--database", default=default_database_path(), help=database_help)
    evidence_search = evidence_sub.add_parser("search", help="search stored evidence")
    evidence_search.add_argument("query")
    evidence_search.add_argument("--repo", help="only evidence for OWNER/REPO")
    evidence_search.add_argument("--type", choices=source_types, dest="source_type")
    evidence_search.add_argument("--top-k", type=int, default=5)
    evidence_search.add_argument("--database", default=default_database_path(), help=database_help)

    init = sub.add_parser("init", help="set up ChangeGuard for a project (run inside the repository)")
    init.add_argument("path", nargs="?", default=".", help="repository path (default: current directory)")
    init.add_argument("--github-action", action="store_true", help="add a workflow that reports on every PR")
    init.add_argument("--action-ref", default="main", help="ChangeGuard git ref the workflow installs (default: main)")
    init.add_argument(
        "--fail-on", type=str.lower, choices=[level.lower() for level in GATE_LEVELS], default="high",
        help="risk level at which the workflow's check fails until a senior approves (default: high)",
    )
    init.add_argument("--no-evidence-folders", action="store_true", help="do not create .changeguard/evidence/")
    init.add_argument("--database", default=default_database_path(), help=database_help)

    sub.add_parser("doctor", help="check your ChangeGuard setup")
    sub.add_parser("ui", help="open the ChangeGuard web app (http://127.0.0.1:8000)")
    sub.add_parser("demo", help="alias for 'ui'")

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


GATE_LEVELS = gate.LEVELS

_PR_URL = re.compile(r"^https?://github\.com/([^/]+)/([^/]+)/pull/(\d+)(?:[/?#].*)?$")


def _parse_pull_request(value: str) -> PullRequestRef:
    match = _PR_URL.match(value.strip())
    if match:
        value = f"{match[1]}/{match[2]}#{match[3]}"
    return PullRequestRef.parse(value)


def _run_demo(_args, _parser) -> None:
    from changeguard.webapp import main as demo_main
    demo_main()


def _run_pr(args, parser) -> None:
    if args.pr_command != "analyze":
        parser.error("use: changeguard pr analyze OWNER/REPO#NUMBER")
    try:
        reference = _parse_pull_request(args.pull_request)
    except ValueError as error:
        parser.error(str(error))
    run = analyze_pull_request(
        reference, database=args.database, llm=args.llm, verify=args.verify,
        llm_provider=args.llm_provider, persist=args.persist, refresh_head=args.refresh_head,
    )
    payload = run.payload
    gate_result = None
    markdown = run.markdown
    if args.fail_on:
        gate_result = gate.evaluate_pull_request(
            reference, payload, args.fail_on, gate.parse_approvers(args.approvers), github_token()
        )
        payload = {**payload, "gate": asdict(gate_result)}
        if markdown:
            markdown = markdown.rstrip() + "\n\n" + gate_result.markdown()
    if args.json:
        _emit(json.dumps(payload, indent=2), args.output)
    elif markdown:
        _emit(markdown, args.output)
    else:
        lines = [f"{payload['status']}: {payload['repository']}#{payload['pull_number']}"]
        error = payload.get("error")
        if error:
            lines.append(f"{error['code']}: {error['message']}")
            if error["code"] in {"NOT_FOUND", "ACCESS_DENIED", "AUTHENTICATION_FAILED", "RATE_LIMITED"} and not run.token_available:
                lines.append("Hint: for private repositories set GITHUB_TOKEN or run `gh auth login`.")
        _emit("\n".join(lines), args.output)
    comment_failed = False
    if args.comment and markdown:
        try:
            url = post_report_comment(reference, markdown, github_token())
            print(f"Posted report comment: {url}", file=sys.stderr)
        except CommentError as error:
            print(f"Could not post the PR comment: {error}", file=sys.stderr)
            comment_failed = True
    if gate_result is not None:
        print(gate_result.summary(), file=sys.stderr)
    if payload["status"] == "FAILED":
        raise SystemExit(2)
    if gate_result is not None and gate_result.blocked:
        raise SystemExit(gate.GATE_EXIT_CODE)
    if comment_failed:
        raise SystemExit(3)


def _run_impact(args, parser) -> None:
    try:
        run = analyze_local(
            args.repo, args.base, args.head, database=args.database, llm=args.llm, verify=args.verify,
            llm_provider=args.llm_provider, max_tool_calls=args.max_tool_calls,
        )
    except (OllamaError, ValueError) as error:
        parser.error(str(error))
    if args.json:
        output = json.dumps(
            {"mode": "llm_enhanced" if args.llm else "deterministic", "analysis": run.payload["analysis"]}, indent=2
        )
    else:
        output = run.markdown or ""
    gate_result = gate.evaluate(run.payload.get("analysis"), args.fail_on) if args.fail_on else None
    if gate_result is not None and not args.json:
        output = output.rstrip() + "\n\n" + gate_result.markdown()
    _emit(output, args.output)
    if gate_result is not None:
        print(gate_result.summary(), file=sys.stderr)
        if gate_result.blocked:
            raise SystemExit(gate.GATE_EXIT_CODE)


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
    if args.evidence_command == "sync":
        from changeguard.project import git_root, sync_evidence

        try:
            root = git_root(args.path)
        except ValueError as error:
            parser.error(str(error))
        repository = (args.repo or repository_key(str(root))).lower()
        added, existing, skipped = sync_evidence(store, root, repository)
        print(f"Evidence for {repository}: {added} added, {existing} already stored.")
        for item in skipped:
            print(f"  skipped {item}")
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
    parser.error("use: changeguard evidence {add,sync,list,search}")


def _run_init(args, parser) -> None:
    from changeguard.project import create_evidence_folders, detect_project, sync_evidence, write_github_action

    try:
        profile = detect_project(args.path)
    except ValueError as error:
        parser.error(str(error))
    root = Path(profile.path)
    repository = repository_key(str(root))
    print(f"ChangeGuard setup for {profile.name}  ({repository})\n")
    languages = ", ".join(f"{name} ({count})" for name, count in profile.languages.items()) or "none detected"
    print(f"Languages:  {languages}")
    print(f"Frameworks: {', '.join(profile.frameworks) or 'none detected'}")
    for note in profile.notes:
        print(f"  note: {note}")
    print()
    if not args.no_evidence_folders:
        for created in create_evidence_folders(root):
            print(f"created  {created}")
    store = SQLiteAnalysisStore(args.database)
    added, existing, skipped = sync_evidence(store, root, repository)
    print(f"evidence {added} document(s) imported, {existing} already stored")
    for item in skipped:
        print(f"  skipped {item}")
    if args.github_action:
        workflow, written = write_github_action(root, ref=args.action_ref, fail_on=args.fail_on)
        relative = workflow.relative_to(root)
        print(f"{'created ' if written else 'exists  '} {relative}")
        if written:
            print("         commit it, then make 'ChangeGuard review gate' a required status check on your main branch")
            print("         (Settings -> Branches); optionally add a GEMINI_API_KEY secret for AI reports")
    HistoryStore(args.database).save_project(str(root), repository, profile.name, profile.languages, profile.frameworks)
    print(f"registered project in {args.database}\n")
    print("Next steps:")
    print("  changeguard impact                 # analyze your latest commit")
    print("  changeguard pr analyze OWNER/REPO#N")
    print("  changeguard ui                     # open the web app")


def _run_doctor(_args, _parser) -> None:
    from changeguard.project import doctor

    problems = 0
    for check in doctor():
        mark = "ok " if check.ok else ("-- " if check.optional else "!! ")
        problems += 0 if check.ok or check.optional else 1
        print(f"{mark} {check.name:<26} {check.detail}")
    if problems:
        raise SystemExit(1)


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
