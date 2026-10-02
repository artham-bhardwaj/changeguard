from __future__ import annotations

import json
import subprocess

from changeguard.analysis import ChangeAnalysisService, ChangeGuardAnalysis
from changeguard.code_intelligence.api import ApiContractMapper, normalize_route, route_shape
from changeguard.code_intelligence.java import JavaSymbolMapper
from changeguard.llm.report import (
    InvestigationReport,
    ReportGenerationResult,
)
from changeguard.risk.engine import RiskEngine
from changeguard.risk.models import BlastRadiusSignals, ChangeContext, ChangeMetrics
from changeguard.storage.sqlite import SQLiteAnalysisStore
from evaluation.runner import EvaluationRunner


def _git(repo, *args: str) -> str:
    result = subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    )
    return result.stdout.strip()


def _repository(tmp_path, before: dict[str, str], after: dict[str, str]):
    repo = tmp_path / "api-repository"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "api-test@example.com")
    _git(repo, "config", "user.name", "API Test")
    for path, content in before.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "baseline")
    base = _git(repo, "rev-parse", "HEAD")
    for path, content in after.items():
        target = repo / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(repo, "add", ".")
    _git(repo, "commit", "-m", "target")
    return repo, base, _git(repo, "rev-parse", "HEAD")


def _symbols(sources: dict[str, str]):
    return JavaSymbolMapper().analyze(sources, (), ()).symbols


def test_spring_endpoint_extraction_supports_mappings_composition_and_aliases():
    source = """package demo;
@RequestMapping(value={"/api//v1/", "/legacy"})
class Controller {
    @GetMapping(path={"//items", "/products/"})
    String get() { return "ok"; }
    @PostMapping("/items") void post() {}
    @PutMapping("/items") void put() {}
    @PatchMapping("/items") void patch() {}
    @DeleteMapping("/items") void delete() {}
    @RequestMapping(value="/explicit", method=RequestMethod.POST) void explicit() {}
    @RequestMapping(value="/many", method={RequestMethod.GET, RequestMethod.PUT}) void many() {}
    @GetMapping String root() { return "ok"; }
    String notAnEndpoint() { return "no"; }
}
"""
    path = "src/main/java/demo/Controller.java"
    endpoints = ApiContractMapper().extract_endpoints(
        {path: source}, _symbols({path: source}), "revision-1"
    )

    identities = {(item.http_method, item.route) for item in endpoints}
    assert {
        ("GET", "/api/v1/items"),
        ("GET", "/api/v1/products"),
        ("GET", "/legacy/items"),
        ("POST", "/api/v1/items"),
        ("PUT", "/api/v1/items"),
        ("PATCH", "/api/v1/items"),
        ("DELETE", "/api/v1/items"),
        ("POST", "/api/v1/explicit"),
        ("GET", "/api/v1/many"),
        ("PUT", "/api/v1/many"),
        ("GET", "/api/v1"),
        ("GET", "/legacy"),
    } <= identities
    assert all(item.framework == "spring" for item in endpoints)
    assert all(item.provider_symbol_id for item in endpoints)
    assert all(item.source_revision == "revision-1" for item in endpoints)
    assert not any("notAnEndpoint" in item.declaring_method for item in endpoints)


def test_request_mapping_without_explicit_method_stays_unknown():
    path = "src/main/java/demo/Controller.java"
    source = """package demo;
class Controller {
    @RequestMapping("/untyped") String request() { return "x"; }
}
"""
    endpoints = ApiContractMapper().extract_endpoints(
        {path: source}, _symbols({path: source})
    )
    assert [(item.http_method, item.route) for item in endpoints] == [
        ("UNKNOWN", "/untyped")
    ]


def test_route_normalization_preserves_case_and_segment_semantics():
    assert normalize_route(" //Api\\\\items//{itemId}/?page=1#top ") == "/Api/items/{itemId}"
    assert normalize_route("items") == "/items"
    assert normalize_route("/") == "/"
    assert route_shape("/users/{id}") == "/users/{}"
    assert route_shape("/Users/{userId}") != route_shape("/users/{id}")


def test_frontend_extraction_handles_fetch_clients_and_static_construction():
    source = """const BASE = "/api";
const API = { summary: BASE + "/summary" };
fetch(API.summary);
fetch("/api/items", { method: "POST" });
axios.get("/api/items");
axios.post("/api/items", payload);
client.put("/api/items");
client.request({ url: "/api/items", method: "PATCH" });
async function fetchJson(url) { return fetch(url); }
fetchJson("/api/summary");
"""
    consumers = ApiContractMapper().extract_consumers(
        {"src/client.ts": source}
    )
    assert [(item.http_method, item.route) for item in consumers] == [
        ("GET", "/api/summary"),
        ("POST", "/api/items"),
        ("GET", "/api/items"),
        ("POST", "/api/items"),
        ("PUT", "/api/items"),
        ("PATCH", "/api/items"),
        ("GET", "/api/summary"),
    ]
    assert all(item.source_language == "typescript" for item in consumers)
    assert all(item.source_line > 0 for item in consumers)


def test_dynamic_urls_are_unresolved_and_unsupported_patterns_do_not_create_consumers():
    source = """// fetch("/not-a-request")
fetch("/api/incidents/" + incidentId);
fetch(dynamicUrl);
xhr.send("/api/incidents");
"""
    consumers = ApiContractMapper().extract_consumers(
        {"src/client.js": source}
    )
    assert len(consumers) == 2
    assert consumers[0].route is None
    assert consumers[0].route_prefix == "/api/incidents"
    assert consumers[1].route is None
    assert consumers[1].route_prefix is None


def test_frontend_snapshot_limits_and_generated_asset_exclusions():
    mapper = ApiContractMapper(max_frontend_files=1, max_file_bytes=64, max_total_bytes=64)
    impact = mapper.analyze(
        java_sources={},
        frontend_sources={
            "src/a.js": 'fetch("/a");',
            "src/b.ts": 'fetch("/b");',
            "dist/app.js": 'fetch("/ignored");',
            "src/app.min.js": 'fetch("/ignored");',
        },
        symbols=(),
        revision="head",
        changed_files=(),
        changed_lines=(),
    )
    assert impact.metrics.frontend_files_analyzed == 1
    assert impact.metrics.omitted_frontend_files == 1
    assert [item.source_file for item in impact.consumers] == ["src/a.js"]


def test_matching_keeps_method_and_route_mismatches_separate_and_uses_path_templates():
    java_sources = {
        "src/main/java/demo/Controller.java": """package demo;
class Controller {
    @GetMapping("/incidents/{id}") String item() { return "x"; }
    @PostMapping("/incidents") String create() { return "x"; }
}
"""
    }
    symbols = _symbols(java_sources)
    frontend = {
        "src/main/js/client.js": """fetch("/incidents/123");
fetch("/incidents");
fetch("/missing");
"""
    }
    impact = ApiContractMapper().analyze(
        java_sources=java_sources,
        frontend_sources=frontend,
        symbols=symbols,
        revision="head",
        changed_files=(),
        changed_lines=(),
    )
    assert len(impact.edges) == 1
    assert impact.edges[0].endpoint_identity == "GET /incidents/{id}"
    assert impact.metrics.unresolved_consumers == 0


def test_ambiguous_routes_do_not_create_edges():
    java_sources = {
        "src/ById.java": 'package demo; class ById { @GetMapping("/items/{id}") String get(){return "";} }',
        "src/ByName.java": 'package demo; class ByName { @GetMapping("/items/{name}") String get(){return "";} }',
    }
    impact = ApiContractMapper().analyze(
        java_sources=java_sources,
        frontend_sources={"src/client.js": 'fetch("/items/123");'},
        symbols=_symbols(java_sources),
        revision="head",
        changed_files=(),
        changed_lines=(),
    )
    assert impact.edges == ()
    assert len(impact.ambiguous_relationships) == 1
    assert len(impact.ambiguous_relationships[0].candidate_endpoint_ids) == 2


def test_exact_route_takes_precedence_over_a_matching_path_template():
    java_sources = {
        "src/Controller.java": """package demo;
class Controller {
    @GetMapping("/incidents/{id}") String byId() { return "item"; }
    @GetMapping("/incidents/summary") String summary() { return "summary"; }
}
"""
    }
    impact = ApiContractMapper().analyze(
        java_sources=java_sources,
        frontend_sources={"src/client.js": 'fetch("/incidents/summary");'},
        symbols=_symbols(java_sources),
        revision="head",
        changed_files=(),
        changed_lines=(),
    )

    assert len(impact.edges) == 1
    assert impact.edges[0].endpoint_identity == "GET /incidents/summary"
    assert impact.ambiguous_relationships == ()


def test_backend_change_includes_known_frontend_consumer_and_risk_signal(tmp_path):
    java_path = "src/main/java/demo/Controller.java"
    js_path = "src/main/resources/static/client.js"
    before = {
        java_path: """package demo;
@RequestMapping("/api")
class Controller {
    @GetMapping("/summary") String getSummary() { return "before"; }
}
""",
        js_path: 'fetch("/api/summary");\n',
    }
    after = {
        java_path: before[java_path].replace('"before"', '"after"'),
        js_path: before[js_path],
    }
    repo, base, head = _repository(tmp_path, before, after)
    store = SQLiteAnalysisStore(str(tmp_path / "analysis.sqlite3"))
    result = ChangeAnalysisService(store).analyze(
        repo_path=str(repo), base=base, head=head
    )

    assert result.api_impact is not None
    assert result.api_impact.changed_endpoint_ids
    assert len(result.api_impact.edges) == 1
    assert result.blast_radius is not None
    assert len(result.blast_radius.cross_language_api_consumers) == 1
    assert js_path in result.blast_radius.affected_files
    assert result.blast_radius.api_edges[0].relationship_type == "API_CONSUMER"
    blast_factor = next(
        item for item in result.risk_assessment.factors if item.factor == "blast_radius"
    )
    assert blast_factor.value["affected_callers"] == 1
    assert any(item.stage == "api_analysis" for item in result.trace)

    saved = store.get_change_analysis(result.analysis_id)
    assert saved is not None
    assert saved["api_impact"]["edges"][0]["relationship_type"] == "API_CONSUMER"
    restored = ChangeGuardAnalysis.from_dict(saved)
    assert restored.api_impact == result.api_impact
    assert restored.blast_radius == result.blast_radius
    assert json.loads(json.dumps(result.to_dict()))["api_impact"] == saved["api_impact"]


def test_changed_frontend_consumer_identifies_backend_provider(tmp_path):
    java_path = "src/main/java/demo/Controller.java"
    js_path = "src/main/js/client.js"
    before = {
        java_path: 'package demo; class Controller { @GetMapping("/current") String current(){return "ok";} }',
        js_path: 'fetch("/old");\n',
    }
    after = {java_path: before[java_path], js_path: 'fetch("/current");\n'}
    repo, base, head = _repository(tmp_path, before, after)
    result = ChangeAnalysisService(
        SQLiteAnalysisStore(str(tmp_path / "analysis.sqlite3"))
    ).analyze(repo_path=str(repo), base=base, head=head)

    assert result.api_impact is not None
    assert result.api_impact.changed_consumer_ids
    assert result.api_impact.providers_for_changed_consumers == (
        f"{java_path}::demo.Controller.current()",
    )
    assert result.blast_radius is not None
    assert result.blast_radius.api_provider_symbols_for_changed_consumers == (
        f"{java_path}::demo.Controller.current()",
    )


def test_cross_language_risk_uses_existing_blast_radius_metrics():
    without_consumer = RiskEngine().assess(
        ChangeContext(
            change=ChangeMetrics(files_changed=1, lines_added=1, lines_deleted=0),
            blast_radius=BlastRadiusSignals(
                directly_affected_symbols=0,
                indirectly_affected_symbols=0,
                affected_files=1,
                dependency_depth=0,
                affected_callers=0,
                affected_components=None,
                unresolved_relationships=0,
            ),
        )
    )
    with_consumer = RiskEngine().assess(
        ChangeContext(
            change=ChangeMetrics(files_changed=1, lines_added=1, lines_deleted=0),
            blast_radius=BlastRadiusSignals(
                directly_affected_symbols=0,
                indirectly_affected_symbols=0,
                affected_files=2,
                dependency_depth=0,
                affected_callers=1,
                affected_components=None,
                unresolved_relationships=0,
            ),
        )
    )
    old_factor = next(item for item in without_consumer.factors if item.factor == "blast_radius")
    new_factor = next(item for item in with_consumer.factors if item.factor == "blast_radius")
    assert new_factor.normalized_value > old_factor.normalized_value
    assert old_factor.weight == new_factor.weight


def test_old_analysis_without_api_impact_remains_deserializable(git_repo, tmp_path):
    repo, base, head = git_repo
    result = ChangeAnalysisService(
        SQLiteAnalysisStore(str(tmp_path / "old-analysis.sqlite3"))
    ).analyze(repo_path=str(repo), base=base, head=head)
    legacy = result.to_dict()
    legacy.pop("api_impact", None)
    if legacy["blast_radius"]:
        for key in (
            "cross_language_api_consumers",
            "api_edges",
            "api_unresolved_relationships",
            "api_ambiguous_relationships",
            "api_provider_symbols_for_changed_consumers",
        ):
            legacy["blast_radius"].pop(key, None)
    restored = ChangeGuardAnalysis.from_dict(legacy)
    assert restored.api_impact is None


def test_api_evaluation_cases_have_independent_ground_truth_and_repeatable_results():
    case_ids = (
        "api-java-to-javascript",
        "api-java-to-typescript",
        "api-multiple-consumers",
        "api-dynamic-url",
        "api-ambiguous-endpoint",
        "api-path-variable",
        "api-unrelated-endpoint",
        "api-changed-backend-impact",
        "api-changed-frontend-consumer",
        "self-healing-summary-api-regression",
    )
    runner = EvaluationRunner()
    for case_id in case_ids:
        first = runner.run_case(case_id, persist=False)
        repeated = runner.run_case(case_id, persist=False)
        assert first.status == "passed", (case_id, first.failures, first.deterministic_metrics)
        assert first.fingerprint == repeated.fingerprint
        assert first.deterministic_metrics["api_impact"]["api_consumer_edges"] is not None


class _CapturingInvestigator:
    def __init__(self):
        self.task = ""

    def investigate_and_report(self, task: str, **_kwargs):
        self.task = task
        return ReportGenerationResult(
            report=InvestigationReport("summary", [], [], [], [], [], []),
        )


def test_llm_context_and_report_use_authoritative_api_edges(tmp_path):
    java_path = "src/main/java/demo/Controller.java"
    js_path = "src/main/js/client.js"
    before = {
        java_path: """package demo;
class Controller {
    @GetMapping("/summary") String summary() { return "before"; }
}
""",
        js_path: 'fetch("/summary");\n',
    }
    after = {
        java_path: before[java_path].replace('"before"', '"after"'),
        js_path: before[js_path],
    }
    repo, base, head = _repository(tmp_path, before, after)
    investigator = _CapturingInvestigator()
    result = ChangeAnalysisService(
        SQLiteAnalysisStore(str(tmp_path / "analysis.sqlite3")),
        investigator=investigator,
    ).analyze(repo_path=str(repo), base=base, head=head)

    assert result.api_impact is not None and len(result.api_impact.edges) == 1
    assert "deterministic_edges" in investigator.task
    assert "do not infer additional API relationships" in investigator.task
    assert result.investigation_report is not None
    assert result.investigation_report.api_impact == result.api_impact.to_dict()


def test_mapping_annotation_change_marks_only_the_affected_endpoint(tmp_path):
    java_path = "src/main/java/demo/Controller.java"
    before = {
        java_path: """package demo;
@RequestMapping("/api")
class Controller {
    @GetMapping("/old") String current() { return "ok"; }
    @PostMapping("/items") String create() { return "ok"; }
}
"""
    }
    after = {
        java_path: before[java_path].replace('GetMapping("/old")', 'GetMapping("/new")')
    }
    repo, base, head = _repository(tmp_path, before, after)
    result = ChangeAnalysisService(
        SQLiteAnalysisStore(str(tmp_path / "mapping-analysis.sqlite3"))
    ).analyze(repo_path=str(repo), base=base, head=head)

    assert result.api_impact is not None
    changed_routes = {
        endpoint.route
        for endpoint in result.api_impact.endpoints
        if endpoint.endpoint_id in result.api_impact.changed_endpoint_ids
    }
    assert changed_routes == {"/api/new"}


def test_class_mapping_annotation_change_marks_all_endpoints_on_that_class(tmp_path):
    java_path = "src/main/java/demo/Controller.java"
    before = {
        java_path: """package demo;
@RequestMapping("/api")
class Controller {
    @GetMapping("/summary") String summary() { return "ok"; }
    @PostMapping("/items") String create() { return "ok"; }
}
"""
    }
    after = {java_path: before[java_path].replace('RequestMapping("/api")', 'RequestMapping("/v2")')}
    repo, base, head = _repository(tmp_path, before, after)
    result = ChangeAnalysisService(
        SQLiteAnalysisStore(str(tmp_path / "class-mapping-analysis.sqlite3"))
    ).analyze(repo_path=str(repo), base=base, head=head)

    assert result.api_impact is not None
    changed_routes = {
        endpoint.route
        for endpoint in result.api_impact.endpoints
        if endpoint.endpoint_id in result.api_impact.changed_endpoint_ids
    }
    assert changed_routes == {"/v2/summary", "/v2/items"}
