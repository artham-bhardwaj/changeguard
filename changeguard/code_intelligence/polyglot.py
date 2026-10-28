"""Dispatch code analysis to the Java, Python and TypeScript/JavaScript analyzers."""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import replace
from typing import Iterable

from changeguard.code_intelligence.api import ApiEndpoint, ApiImpact
from changeguard.code_intelligence.graph import merge_analyses
from changeguard.code_intelligence.java import ChangedLineRange, JavaAnalysis, JavaSymbolMapper
from changeguard.code_intelligence.python_lang import (
    PythonSymbolMapper,
    extract_python_endpoints,
    is_python_source,
)
from changeguard.code_intelligence.script_lang import ScriptSymbolMapper, is_script_source


def is_java_source(path: str) -> bool:
    return path.endswith(".java")


def is_code_source(path: str) -> bool:
    return is_java_source(path) or is_python_source(path) or is_script_source(path)


class PolyglotSymbolMapper:
    """Same interface as ``JavaSymbolMapper``; Java-only input gives identical results."""

    def __init__(self, java: JavaSymbolMapper | None = None) -> None:
        self.java = java or JavaSymbolMapper()
        self.python = PythonSymbolMapper()
        self.script = ScriptSymbolMapper()

    def analyze(
        self,
        sources: dict[str, str],
        changed_ranges: Iterable[ChangedLineRange],
        changed_files: Iterable[str],
        *,
        omitted_sources: int = 0,
        omitted_python: int = 0,
        omitted_script: int = 0,
    ) -> JavaAnalysis:
        changed_ranges = tuple(changed_ranges)
        changed_files = tuple(changed_files)
        java = {path: text for path, text in sources.items() if is_java_source(path)}
        python = {path: text for path, text in sources.items() if is_python_source(path)}
        script = {path: text for path, text in sources.items() if is_script_source(path)}
        parts: list[JavaAnalysis] = []
        if java or omitted_sources or any(is_java_source(path) for path in changed_files) or not (python or script):
            parts.append(self.java.analyze(java, changed_ranges, changed_files, omitted_sources=omitted_sources))
        if python or omitted_python:
            parts.append(self.python.analyze(python, changed_ranges, changed_files, omitted_sources=omitted_python))
        if script or omitted_script:
            parts.append(self.script.analyze(script, changed_ranges, changed_files, omitted_sources=omitted_script))
        return merge_analyses(parts)

    def endpoints(self, sources: dict[str, str], revision: str) -> tuple[ApiEndpoint, ...]:
        """Backend endpoints declared in Python (FastAPI/Flask) and JS/TS (Express, Next.js)."""
        python = {path: text for path, text in sources.items() if is_python_source(path)}
        script = {path: text for path, text in sources.items() if is_script_source(path)}
        found: list[ApiEndpoint] = []
        if python:
            found.extend(extract_python_endpoints(self.python.parse_project(python), revision))
        if script:
            found.extend(self.script.extract_endpoints(self.script.parse_project(script), revision))
        return tuple(found)


def propagate_through_api(analysis: JavaAnalysis, api_impact: ApiImpact | None) -> JavaAnalysis:
    """Add the frontend functions that call changed endpoints, and their callers, as dependents.

    Edges never cross languages inside a call graph; the API match is the bridge from a changed
    backend endpoint to the frontend code that requests it.
    """
    if api_impact is None or not api_impact.impacted_consumer_ids:
        return analysis
    consumers = {item.consumer_id: item for item in api_impact.consumers}
    symbols_by_file = defaultdict(list)
    for symbol in analysis.symbols:
        symbols_by_file[symbol.file_path].append(symbol)
    known = set(analysis.changed_symbol_ids) | set(analysis.direct_dependents) | set(analysis.indirect_dependents)
    seeds: set[str] = set()
    for consumer_id in api_impact.impacted_consumer_ids:
        consumer = consumers.get(consumer_id)
        if consumer is None:
            continue
        enclosing = [
            symbol for symbol in symbols_by_file.get(consumer.source_file, [])
            if symbol.start_line <= consumer.source_line <= symbol.end_line and symbol.kind != "type"
        ]
        if enclosing:
            innermost = max(enclosing, key=lambda symbol: symbol.start_line)
            if innermost.symbol_id not in known:
                seeds.add(innermost.symbol_id)
    if not seeds:
        return analysis
    reverse_edges = defaultdict(set)
    for edge in analysis.call_edges:
        if edge.target_id and edge.target_id != edge.caller_id:
            reverse_edges[edge.target_id].add(edge.caller_id)
    reached = set(seeds)
    queue = deque(seeds)
    while queue:
        current = queue.popleft()
        for caller in reverse_edges.get(current, ()):
            if caller not in reached and caller not in known:
                reached.add(caller)
                queue.append(caller)
    by_id = {symbol.symbol_id: symbol for symbol in analysis.symbols}
    reached_files = {by_id[item].file_path for item in reached if item in by_id}
    return replace(
        analysis,
        indirect_dependents=tuple(sorted(set(analysis.indirect_dependents) | reached)),
        affected_files=tuple(sorted(set(analysis.affected_files) | reached_files)),
        affected_packages=tuple(sorted(
            set(analysis.affected_packages)
            | {by_id[item].package_name for item in reached if item in by_id and by_id[item].package_name}
        )),
        maximum_dependency_depth=max(analysis.maximum_dependency_depth or 0, 2),
    )
