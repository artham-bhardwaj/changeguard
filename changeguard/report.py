"""Render a ChangeGuard analysis as a readable Markdown report."""
from __future__ import annotations

from typing import Any

MAX_LISTED_FILES = 20
MAX_LISTED_ITEMS = 10


def render_markdown(analysis: dict[str, Any], *, pr: dict[str, Any] | None = None) -> str:
    """Render an analysis dict (``ChangeGuardAnalysis.to_dict()``) as Markdown.

    ``pr`` is the surrounding ``PRAnalysisResult.to_dict()`` when the analysis came from a PR.
    """
    lines: list[str] = []
    add = lines.append

    if pr:
        add(f"# ChangeGuard: {pr['repository']}#{pr['pull_number']}")
        add("")
        add(f"- **Status:** {pr['status']}")
        if pr.get("base_sha") and pr.get("head_sha"):
            add(f"- **Revisions:** `{_short(pr['base_sha'])}..{_short(pr['head_sha'])}`")
        if pr.get("status") == "STALE" and pr.get("current_head_sha"):
            add(f"- **Warning:** the PR head moved to `{_short(pr['current_head_sha'])}` during analysis; re-run for the latest code.")
    else:
        add("# ChangeGuard change report")
        add("")
        add(f"- **Revisions:** `{_short_range(analysis.get('change_reference', ''))}`")
    add(f"- **Summary:** {analysis.get('change_summary', '')}")
    add("")

    _risk_section(analysis.get("risk_assessment") or {}, add)
    _changed_code_section(analysis, add)
    _api_section(analysis.get("api_impact") or {}, add)
    _history_section(analysis.get("historical_evidence") or {}, add)
    _ai_section(analysis, add)
    _verification_section(analysis, add)

    checks = _unique(analysis.get("recommended_verification") or [])
    if checks:
        add("## Recommended checks")
        add("")
        lines.extend(f"- [ ] {item}" for item in checks)
        add("")

    uncertainties = [
        item for item in _unique(analysis.get("uncertainties") or [])
        if item != "NO_RELEVANT_EVIDENCE"
    ]
    if uncertainties:
        add("## Limitations")
        add("")
        lines.extend(f"- {item}" for item in uncertainties[:MAX_LISTED_ITEMS])
        if len(uncertainties) > MAX_LISTED_ITEMS:
            add(f"- …and {len(uncertainties) - MAX_LISTED_ITEMS} more (see `--json` output)")
        add("")
    return "\n".join(lines).rstrip() + "\n"


def _risk_section(risk: dict[str, Any], add) -> None:
    if not risk:
        return
    metrics = risk.get("metrics") or {}
    coverage = metrics.get("score_weight_coverage")
    coverage_text = f" · based on {coverage:.0%} of signals" if isinstance(coverage, (int, float)) else ""
    add(f"## Risk: {risk.get('overall_level', 'UNKNOWN')} ({risk.get('score', 0):.1f} / 100{coverage_text})")
    add("")
    factors = risk.get("factors") or []
    if factors:
        add("| Factor | Signal | Contribution |")
        add("| --- | --- | --- |")
        for factor in factors:
            contribution = factor.get("contribution")
            value = f"{contribution:.1f} of {factor.get('weight', 0):.0f}" if isinstance(contribution, (int, float)) else "not scored"
            add(f"| {factor.get('factor', '').replace('_', ' ')} | {factor.get('state', '')} | {value} |")
        add("")


def _changed_code_section(analysis: dict[str, Any], add) -> None:
    files = analysis.get("changed_files") or []
    add(f"## Changed code ({len(files)} file{'s' if len(files) != 1 else ''})")
    add("")
    for path in files[:MAX_LISTED_FILES]:
        add(f"- `{path}`")
    if len(files) > MAX_LISTED_FILES:
        add(f"- …and {len(files) - MAX_LISTED_FILES} more")
    methods = [
        symbol.get("qualified_name", "")
        for symbol in analysis.get("changed_symbols") or []
        if symbol.get("kind") in {"method", "constructor"}
    ]
    if methods:
        add("")
        add("Changed methods:")
        for name in methods[:MAX_LISTED_ITEMS]:
            add(f"- `{name}`")
        if len(methods) > MAX_LISTED_ITEMS:
            add(f"- …and {len(methods) - MAX_LISTED_ITEMS} more")
    blast = analysis.get("blast_radius") or {}
    direct = len(blast.get("direct_dependents") or [])
    indirect = len(blast.get("indirect_dependents") or [])
    if direct or indirect:
        add("")
        add(f"Dependents: {direct} direct, {indirect} indirect.")
    add("")


def _api_section(api: dict[str, Any], add) -> None:
    if not api or not api.get("endpoints"):
        return
    endpoints = {item["endpoint_id"]: item for item in api.get("endpoints") or []}
    consumers = {item["consumer_id"]: item for item in api.get("consumers") or []}
    changed = api.get("changed_endpoint_ids") or []
    impacted = set(api.get("impacted_consumer_ids") or [])
    add("## API impact")
    add("")
    metrics = api.get("metrics") or {}
    add(
        f"{metrics.get('endpoints_discovered', len(endpoints))} endpoints, "
        f"{metrics.get('consumers_discovered', len(consumers))} frontend calls, "
        f"{metrics.get('deterministic_matches', 0)} matched."
    )
    add("")
    if not changed:
        add("No changed API endpoints.")
        add("")
        return
    for endpoint_id in changed:
        endpoint = endpoints.get(endpoint_id, {})
        identity = f"{endpoint.get('http_method', '?')} {endpoint.get('route', endpoint_id)}"
        add(f"- **Changed endpoint `{identity}`** ({endpoint.get('declaring_method', 'unknown method')})")
        callers = [
            consumers[edge["target_node"]]
            for edge in api.get("edges") or []
            if edge.get("endpoint_id") == endpoint_id
            and edge.get("target_node") in impacted
            and edge.get("target_node") in consumers
        ]
        if callers:
            for consumer in callers:
                add(f"  - called from `{consumer['source_file']}:{consumer['source_line']}`")
        else:
            add("  - no frontend callers found")
    add("")


def _history_section(history: dict[str, Any], add) -> None:
    results = history.get("results") or []
    if not results:
        return
    add("## Related history")
    add("")
    for item in results:
        add(
            f"- [{item.get('source_type', 'evidence')}] **{item.get('title', '')}** "
            f"(match {item.get('similarity_score', 0):.2f}) — `{item.get('source', '')}`"
        )
    add("")


def _ai_section(analysis: dict[str, Any], add) -> None:
    report = analysis.get("investigation_report")
    error = analysis.get("investigation_error")
    state = analysis.get("investigation_state")
    if report is None and error is None and state is None:
        return
    add("## AI investigation")
    add("")
    if report is None:
        add(f"Unavailable: {error or 'no report was produced'}. The deterministic analysis above is unaffected.")
        add("")
        return
    add(report.get("summary", ""))
    add("")
    findings = report.get("findings") or []
    if findings:
        add("Findings:")
        for finding in findings:
            refs = ", ".join(finding.get("evidence_refs") or [])
            add(f"- {finding.get('statement', '')}" + (f" _(evidence: {refs})_" if refs else ""))
        add("")
    unknowns = report.get("uncertainties") or []
    if unknowns:
        add("Unknowns:")
        lines = [f"- {item}" for item in _unique(unknowns)]
        for line in lines:
            add(line)
        add("")


def _verification_section(analysis: dict[str, Any], add) -> None:
    plan = analysis.get("verification_plan")
    result = analysis.get("verification_result")
    if plan is None and result is None:
        return
    add("## Verification")
    add("")
    if result:
        add(f"**{result.get('status')}** in {result.get('duration_seconds', 0):.1f}s")
        for check in result.get("checks") or []:
            spec = check.get("check") or {}
            targets = ", ".join(spec.get("targets") or [])
            add(f"- {check.get('status')}: {spec.get('operation')} {targets}".rstrip())
            if check.get("failure_summary"):
                add(f"  - {check['failure_summary'][:300]}")
        for item in result.get("limitations") or []:
            add(f"- {item}")
    elif plan:
        add(f"Plan status: {plan.get('status')}")
        for item in plan.get("limitations") or []:
            add(f"- {item}")
    add("")


def _unique(items: list[str]) -> list[str]:
    seen: set[str] = set()
    result = []
    for item in items:
        if item not in seen:
            seen.add(item)
            result.append(item)
    return result


def _short(sha: str) -> str:
    return sha[:12]


def _short_range(reference: str) -> str:
    base, separator, head = reference.partition("..")
    return f"{_short(base)}..{_short(head)}" if separator else reference
