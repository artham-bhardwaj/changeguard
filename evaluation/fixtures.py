"""Small authored golden fixtures. Expected values are independent of the analyzer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from changeguard.evidence.models import EvidenceDocument
from evaluation.models import EvaluationCase


@dataclass(frozen=True)
class FixtureDefinition:
    case: EvaluationCase
    baseline_files: dict[str, str]
    target_files: dict[str, str]
    evidence: tuple[EvidenceDocument, ...] = ()


def _java_fixture(case_id: str, before: str, after: str, path: str = "src/main/java/demo/Flow.java") -> tuple[dict[str, str], dict[str, str]]:
    return {path: before}, {path: after}


_service_before = """package demo;
public class Service {
    public int calculate() {
        return 1;
    }
}
"""
_service_after = _service_before.replace("return 1;", "return 2;")
_caller = """package demo;
public class Caller {
    Service service;
    public int caller() {
        return service.calculate();
    }
}
"""
_terminal_before = """package demo;
public class Terminal {
    public int terminal() {
        return 1;
    }
}
"""
_terminal_after = _terminal_before.replace("return 1;", "return 2;")
_middle = """package demo;
public class Middle {
    Terminal terminal;
    public int middle() {
        return terminal.terminal();
    }
}
"""
_entry = """package demo;
public class Entry {
    Middle middle;
    public int entry() {
        return middle.middle();
    }
}
"""
_c_before = """package demo;
class Worker {
    int work() { return 1; }
}
"""
_c_after = """package demo;
class Worker {
    int work() {
        externalDependency();
        return 2;
    }
}
"""
_f_before = """// baseline note




package demo;
class Unmapped {
    int value() { return 1; }
}
"""
_f_after = _f_before.replace("baseline note", "updated note")

_definitions = [
    FixtureDefinition(
        EvaluationCase(
            "local-method-change",
            "local-method-change",
            changed_files=("src/main/java/demo/Service.java",),
            changed_symbols=("demo.Service", "demo.Service.calculate()"),
            expected_impacted_symbols=(
                "src/main/java/demo/Caller.java::demo.Caller.caller()",
            ),
            metadata={"scenario": "direct-call"},
        ),
        {
            "src/main/java/demo/Service.java": _service_before,
            "src/main/java/demo/Caller.java": _caller,
        },
        {
            "src/main/java/demo/Service.java": _service_after,
            "src/main/java/demo/Caller.java": _caller,
        },
    ),
    FixtureDefinition(
        EvaluationCase(
            "multi-hop-dependency",
            "multi-hop-dependency",
            changed_files=("src/main/java/demo/Terminal.java",),
            changed_symbols=("demo.Terminal", "demo.Terminal.terminal()"),
            expected_impacted_symbols=(
                "src/main/java/demo/Middle.java::demo.Middle.middle()",
                "src/main/java/demo/Entry.java::demo.Entry.entry()",
            ),
            metadata={"scenario": "transitive-call-chain"},
        ),
        {
            "src/main/java/demo/Terminal.java": _terminal_before,
            "src/main/java/demo/Middle.java": _middle,
            "src/main/java/demo/Entry.java": _entry,
        },
        {
            "src/main/java/demo/Terminal.java": _terminal_after,
            "src/main/java/demo/Middle.java": _middle,
            "src/main/java/demo/Entry.java": _entry,
        },
    ),
    FixtureDefinition(
        EvaluationCase(
            "unresolved-dependency",
            "unresolved-dependency",
            changed_files=("src/main/java/demo/Flow.java",),
            changed_symbols=("demo.Worker", "demo.Worker.work()"),
            expected_impacted_symbols=(),
            expected_unresolved_dependencies=1,
            expected_uncertainty=True,
            metadata={"scenario": "unresolved-call"},
        ),
        *_java_fixture("unresolved-dependency", _c_before, _c_after),
    ),
    FixtureDefinition(
        EvaluationCase(
            "historical-evidence",
            "historical-evidence",
            changed_files=("src/main/java/demo/Service.java",),
            changed_symbols=("demo.Service", "demo.Service.calculate()"),
            expected_impacted_symbols=(
                "src/main/java/demo/Caller.java::demo.Caller.caller()",
            ),
            expected_evidence_ids=("HISTORY-PAYMENTS-001",),
            metadata={"scenario": "historical-evidence-provenance"},
        ),
        {
            "src/main/java/demo/Service.java": _service_before,
            "src/main/java/demo/Caller.java": _caller,
        },
        {
            "src/main/java/demo/Service.java": _service_after,
            "src/main/java/demo/Caller.java": _caller,
        },
        evidence=(
            EvidenceDocument(
                title="Payment calculation incident",
                source_type="incident",
                source="INC-EVAL-001",
                repository="fixture",
                content="The Service calculate payment amount change caused caller payment totals to be incorrect.",
                created_at="2025-01-01T00:00:00+00:00",
                evidence_id="HISTORY-PAYMENTS-001",
            ),
        ),
    ),
    FixtureDefinition(
        EvaluationCase(
            "verification-pass",
            "verification-pass",
            changed_files=("app.py",),
            expected_verification_properties={
                "verification_result.status": "PASSED",
                "test_signals.state": "known",
            },
            expected_risk_properties={"test_signals.state": "known"},
            metadata={"scenario": "pytest-pass"},
        ),
        {
            "pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
            "app.py": "VALUE = 1\n",
            "tests/test_app.py": "from app import VALUE\n\ndef test_value():\n    assert VALUE == 2\n",
        },
        {
            "pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
            "app.py": "VALUE = 2\n",
            "tests/test_app.py": "from app import VALUE\n\ndef test_value():\n    assert VALUE == 2\n",
        },
    ),
    FixtureDefinition(
        EvaluationCase(
            "verification-fail",
            "verification-fail",
            changed_files=("app.py",),
            expected_verification_properties={
                "verification_result.status": "FAILED",
                "test_signals.state": "known",
            },
            expected_risk_properties={"test_signals.state": "known"},
            metadata={"scenario": "pytest-failure"},
        ),
        {
            "pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
            "app.py": "VALUE = 1\n",
            "tests/test_app.py": "from app import VALUE\n\ndef test_value():\n    assert VALUE == 2\n",
        },
        {
            "pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
            "app.py": "VALUE = 3\n",
            "tests/test_app.py": "from app import VALUE\n\ndef test_value():\n    assert VALUE == 2\n",
        },
    ),
    FixtureDefinition(
        EvaluationCase(
            "verification-not-discovered",
            "verification-not-discovered",
            changed_files=("app.py",),
            expected_verification_properties={
                "verification_result.status": "PASSED",
                "test_signals.state": "unknown",
            },
            expected_risk_properties={"test_signals.state": "unknown"},
            metadata={"scenario": "no-confident-test-map"},
        ),
        {
            "pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
            "app.py": "VALUE = 1\n",
            "tests/test_other.py": "def test_other():\n    assert True\n",
        },
        {
            "pyproject.toml": '[tool.pytest.ini_options]\ntestpaths = ["tests"]\n',
            "app.py": "VALUE = 2\n",
            "tests/test_other.py": "def test_other():\n    assert True\n",
        },
    ),
    FixtureDefinition(
        EvaluationCase(
            "unmapped-change",
            "unmapped-change",
            changed_files=("src/main/java/demo/Flow.java",),
            changed_symbols=(),
            expected_impacted_symbols=(),
            expected_unmapped_changes=1,
            expected_uncertainty=True,
            metadata={"scenario": "comment-only-edit"},
        ),
        *_java_fixture("unmapped-change", _f_before, _f_after),
    ),
]


def _api_fixture(
    case_id: str,
    *,
    java_before: dict[str, str],
    java_after: dict[str, str],
    frontend_before: dict[str, str],
    frontend_after: dict[str, str],
    changed_files: tuple[str, ...],
    expected_api_endpoints: tuple[str, ...],
    expected_api_consumers: tuple[str, ...],
    expected_api_edges: tuple[str, ...],
    expected_api_impacted_consumers: tuple[str, ...] | None = None,
    expected_api_providers_for_changed_consumers: tuple[str, ...] | None = None,
    expected_ambiguous_matches: int = 0,
    expected_unresolved_consumers: int = 0,
    expected_dynamic_url_count: int = 0,
    scenario: str,
) -> FixtureDefinition:
    case = EvaluationCase(
        case_id=case_id,
        repository_fixture=case_id,
        changed_files=changed_files,
        expected_api_endpoints=expected_api_endpoints,
        expected_api_consumers=expected_api_consumers,
        expected_api_edges=expected_api_edges,
        expected_api_impacted_consumers=expected_api_impacted_consumers,
        expected_api_providers_for_changed_consumers=(
            expected_api_providers_for_changed_consumers
        ),
        expected_ambiguous_matches=expected_ambiguous_matches,
        expected_unresolved_consumers=expected_unresolved_consumers,
        expected_dynamic_url_count=expected_dynamic_url_count,
        metadata={"scenario": scenario},
    )
    baseline = {**java_before, **frontend_before}
    target = {**java_after, **frontend_after}
    return FixtureDefinition(case, baseline, target)


_API_JAVA_PATH = "src/main/java/demo/Controller.java"
_API_JS_PATH = "src/main/resources/static/client.js"
_API_TS_PATH = "src/main/ts/client.ts"
_SIMPLE_CONTROLLER_BEFORE = """package demo;
@RestController
@RequestMapping("/api")
class Controller {
    @GetMapping("/a")
    String get() { return "before"; }
}
"""
_SIMPLE_CONTROLLER_AFTER = _SIMPLE_CONTROLLER_BEFORE.replace(
    'return "before";', 'return "after";'
)

_definitions.extend(
    (
        _api_fixture(
            "api-java-to-javascript",
            java_before={_API_JAVA_PATH: _SIMPLE_CONTROLLER_BEFORE},
            java_after={_API_JAVA_PATH: _SIMPLE_CONTROLLER_AFTER},
            frontend_before={_API_JS_PATH: 'fetch("/api/a");\n'},
            frontend_after={_API_JS_PATH: 'fetch("/api/a");\n'},
            changed_files=(_API_JAVA_PATH,),
            expected_api_endpoints=("GET /api/a|demo.Controller.get()",),
            expected_api_consumers=(f"{_API_JS_PATH}|GET /api/a",),
            expected_api_edges=(f"GET /api/a|{_API_JS_PATH}|/api/a",),
            expected_api_impacted_consumers=(f"{_API_JS_PATH}|GET /api/a",),
            scenario="spring-controller-to-javascript-fetch",
        ),
        _api_fixture(
            "api-java-to-typescript",
            java_before={
                _API_JAVA_PATH: _SIMPLE_CONTROLLER_BEFORE.replace('"/a"', '"/b"')
            },
            java_after={
                _API_JAVA_PATH: _SIMPLE_CONTROLLER_AFTER.replace('"/a"', '"/b"')
            },
            frontend_before={_API_TS_PATH: 'client.get("/api/b");\n'},
            frontend_after={_API_TS_PATH: 'client.get("/api/b");\n'},
            changed_files=(_API_JAVA_PATH,),
            expected_api_endpoints=("GET /api/b|demo.Controller.get()",),
            expected_api_consumers=(f"{_API_TS_PATH}|GET /api/b",),
            expected_api_edges=(f"GET /api/b|{_API_TS_PATH}|/api/b",),
            expected_api_impacted_consumers=(f"{_API_TS_PATH}|GET /api/b",),
            scenario="spring-controller-to-typescript-client",
        ),
        _api_fixture(
            "api-multiple-consumers",
            java_before={
                _API_JAVA_PATH: _SIMPLE_CONTROLLER_BEFORE.replace('"/a"', '"/c"')
            },
            java_after={
                _API_JAVA_PATH: _SIMPLE_CONTROLLER_AFTER.replace('"/a"', '"/c"')
            },
            frontend_before={
                _API_JS_PATH: 'fetch("/api/c");\n',
                "src/main/ts/other.ts": 'axios.get("/api/c");\n',
            },
            frontend_after={
                _API_JS_PATH: 'fetch("/api/c");\n',
                "src/main/ts/other.ts": 'axios.get("/api/c");\n',
            },
            changed_files=(_API_JAVA_PATH,),
            expected_api_endpoints=("GET /api/c|demo.Controller.get()",),
            expected_api_consumers=(
                f"{_API_JS_PATH}|GET /api/c",
                "src/main/ts/other.ts|GET /api/c",
            ),
            expected_api_edges=(
                f"GET /api/c|{_API_JS_PATH}|/api/c",
                "GET /api/c|src/main/ts/other.ts|/api/c",
            ),
            expected_api_impacted_consumers=(
                f"{_API_JS_PATH}|GET /api/c",
                "src/main/ts/other.ts|GET /api/c",
            ),
            scenario="one-endpoint-multiple-consumers",
        ),
        _api_fixture(
            "api-dynamic-url",
            java_before={
                _API_JAVA_PATH: """package demo;
@RestController
class Controller {
    @GetMapping("/api/d/{id}")
    String get() { return "before"; }
}
"""
            },
            java_after={
                _API_JAVA_PATH: """package demo;
@RestController
class Controller {
    @GetMapping("/api/d/{id}")
    String get() { return "after"; }
}
"""
            },
            frontend_before={_API_JS_PATH: 'fetch("/api/d/" + incidentId);\n'},
            frontend_after={_API_JS_PATH: 'fetch("/api/d/" + incidentId);\n'},
            changed_files=(_API_JAVA_PATH,),
            expected_api_endpoints=("GET /api/d/{id}|demo.Controller.get()",),
            expected_api_consumers=(f"{_API_JS_PATH}|GET DYNAMIC",),
            expected_api_edges=(),
            expected_ambiguous_matches=0,
            expected_unresolved_consumers=1,
            expected_dynamic_url_count=1,
            scenario="dynamic-url-prefix-is-unresolved",
        ),
        _api_fixture(
            "api-ambiguous-endpoint",
            java_before={
                "src/main/java/demo/ById.java": """package demo;
class ById {
    @GetMapping("/api/items/{id}")
    String get() { return "before"; }
}
""",
                "src/main/java/demo/ByName.java": """package demo;
class ByName {
    @GetMapping("/api/items/{name}")
    String get() { return "name"; }
}
""",
            },
            java_after={
                "src/main/java/demo/ById.java": """package demo;
class ById {
    @GetMapping("/api/items/{id}")
    String get() { return "after"; }
}
""",
                "src/main/java/demo/ByName.java": """package demo;
class ByName {
    @GetMapping("/api/items/{name}")
    String get() { return "name"; }
}
""",
            },
            frontend_before={_API_JS_PATH: 'fetch("/api/items/123");\n'},
            frontend_after={_API_JS_PATH: 'fetch("/api/items/123");\n'},
            changed_files=("src/main/java/demo/ById.java",),
            expected_api_endpoints=(
                "GET /api/items/{id}|demo.ById.get()",
                "GET /api/items/{name}|demo.ByName.get()",
            ),
            expected_api_consumers=(f"{_API_JS_PATH}|GET /api/items/123",),
            expected_api_edges=(),
            expected_ambiguous_matches=1,
            expected_unresolved_consumers=0,
            scenario="multiple-route-template-matches-remain-ambiguous",
        ),
        _api_fixture(
            "api-path-variable",
            java_before={
                _API_JAVA_PATH: """package demo;
class Controller {
    @GetMapping("/incidents/{id}")
    String get() { return "before"; }
}
"""
            },
            java_after={
                _API_JAVA_PATH: """package demo;
class Controller {
    @GetMapping("/incidents/{id}")
    String get() { return "after"; }
}
"""
            },
            frontend_before={_API_JS_PATH: 'fetch("/incidents/123");\n'},
            frontend_after={_API_JS_PATH: 'fetch("/incidents/123");\n'},
            changed_files=(_API_JAVA_PATH,),
            expected_api_endpoints=("GET /incidents/{id}|demo.Controller.get()",),
            expected_api_consumers=(f"{_API_JS_PATH}|GET /incidents/123",),
            expected_api_edges=(f"GET /incidents/{{id}}|{_API_JS_PATH}|/incidents/123",),
            expected_api_impacted_consumers=(f"{_API_JS_PATH}|GET /incidents/123",),
            scenario="one-segment-path-variable-matches-concrete-path",
        ),
        _api_fixture(
            "api-unrelated-endpoint",
            java_before={
                _API_JAVA_PATH: _SIMPLE_CONTROLLER_BEFORE.replace('"/a"', '"/known"')
            },
            java_after={
                _API_JAVA_PATH: _SIMPLE_CONTROLLER_AFTER.replace('"/a"', '"/known"')
            },
            frontend_before={_API_JS_PATH: 'fetch("/unrelated");\n'},
            frontend_after={_API_JS_PATH: 'fetch("/unrelated");\n'},
            changed_files=(_API_JAVA_PATH,),
            expected_api_endpoints=("GET /api/known|demo.Controller.get()",),
            expected_api_consumers=(f"{_API_JS_PATH}|GET /unrelated",),
            expected_api_edges=(),
            expected_api_impacted_consumers=(),
            scenario="unrelated-http-path-does-not-create-edge",
        ),
        _api_fixture(
            "api-changed-backend-impact",
            java_before={_API_JAVA_PATH: _SIMPLE_CONTROLLER_BEFORE},
            java_after={_API_JAVA_PATH: _SIMPLE_CONTROLLER_AFTER},
            frontend_before={_API_JS_PATH: 'fetch("/api/a");\n'},
            frontend_after={_API_JS_PATH: 'fetch("/api/a");\n'},
            changed_files=(_API_JAVA_PATH,),
            expected_api_endpoints=("GET /api/a|demo.Controller.get()",),
            expected_api_consumers=(f"{_API_JS_PATH}|GET /api/a",),
            expected_api_edges=(f"GET /api/a|{_API_JS_PATH}|/api/a",),
            expected_api_impacted_consumers=(f"{_API_JS_PATH}|GET /api/a",),
            scenario="changed-backend-provider-propagates-to-consumer",
        ),
        _api_fixture(
            "api-changed-frontend-consumer",
            java_before={
                _API_JAVA_PATH: """package demo;
class Controller {
    @GetMapping("/api/current")
    String current() { return "stable"; }
}
"""
            },
            java_after={
                _API_JAVA_PATH: """package demo;
class Controller {
    @GetMapping("/api/current")
    String current() { return "stable"; }
}
"""
            },
            frontend_before={_API_JS_PATH: 'fetch("/api/old");\n'},
            frontend_after={_API_JS_PATH: 'fetch("/api/current");\n'},
            changed_files=(_API_JS_PATH,),
            expected_api_endpoints=("GET /api/current|demo.Controller.current()",),
            expected_api_consumers=(f"{_API_JS_PATH}|GET /api/current",),
            expected_api_edges=(f"GET /api/current|{_API_JS_PATH}|/api/current",),
            expected_api_providers_for_changed_consumers=(
                f"{_API_JAVA_PATH}::demo.Controller.current()",
            ),
            scenario="changed-consumer-identifies-provider",
        ),
        _api_fixture(
            "self-healing-summary-api-regression",
            java_before={
                "self-healing-starter/src/main/java/com/youorg/selfhealing/incident/IncidentController.java": """package com.youorg.selfhealing.incident;
@RestController
@RequestMapping("/self-healing/incidents")
class IncidentController {
    @GetMapping("/summary")
    Map<String, Integer> getSummary() {
        return Map.of("resolvedIncidents", 0);
    }
}
"""
            },
            java_after={
                "self-healing-starter/src/main/java/com/youorg/selfhealing/incident/IncidentController.java": """package com.youorg.selfhealing.incident;
@RestController
@RequestMapping("/self-healing/incidents")
class IncidentController {
    @GetMapping("/summary")
    Map<String, Integer> getSummary() {
        return Map.of("resolvedIncidents", 1);
    }
}
"""
            },
            frontend_before={
                "sample-application/src/main/resources/static/ops-console.js": """const API = { summary: '/self-healing/incidents/summary' };
async function fetchJson(url) {
  const response = await fetch(url);
  return response.json();
}
fetchJson(API.summary);
"""
            },
            frontend_after={
                "sample-application/src/main/resources/static/ops-console.js": """const API = { summary: '/self-healing/incidents/summary' };
async function fetchJson(url) {
  const response = await fetch(url);
  return response.json();
}
fetchJson(API.summary);
"""
            },
            changed_files=(
                "self-healing-starter/src/main/java/com/youorg/selfhealing/incident/IncidentController.java",
            ),
            expected_api_endpoints=(
                "GET /self-healing/incidents/summary|"
                "com.youorg.selfhealing.incident.IncidentController.getSummary()",
            ),
            expected_api_consumers=(
                "sample-application/src/main/resources/static/ops-console.js|"
                "GET /self-healing/incidents/summary",
            ),
            expected_api_edges=(
                "GET /self-healing/incidents/summary|"
                "sample-application/src/main/resources/static/ops-console.js|"
                "/self-healing/incidents/summary",
            ),
            expected_api_impacted_consumers=(
                "sample-application/src/main/resources/static/ops-console.js|"
                "GET /self-healing/incidents/summary",
            ),
            scenario="real-pr-controller-summary-to-operations-console",
        ),
    )
)

FIXTURES: dict[str, FixtureDefinition] = {item.case.case_id: item for item in _definitions}
CASES: tuple[EvaluationCase, ...] = tuple(item.case for item in _definitions)


def get_fixture(case_id: str) -> FixtureDefinition:
    try:
        return FIXTURES[case_id]
    except KeyError as error:
        raise ValueError(f"Unknown evaluation case: {case_id}") from error


def list_cases() -> tuple[EvaluationCase, ...]:
    return CASES
