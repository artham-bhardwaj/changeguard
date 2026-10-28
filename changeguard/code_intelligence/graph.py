"""Language-neutral symbol mapping and blast-radius computation.

Language analyzers produce symbols and call edges; this module maps changed lines to
symbols and walks reverse call edges to find direct and indirect dependents. The result
uses the same ``JavaAnalysis`` shape as the Java analyzer so the rest of the pipeline is
unchanged.
"""
from __future__ import annotations

import time
from collections import defaultdict, deque
from typing import Callable, Iterable

from changeguard.code_intelligence.java import (
    ChangedLineRange,
    JavaAnalysis,
    JavaCallEdge,
    JavaSymbol,
    line_range_intersects,
)


def build_analysis(
    *,
    language: str,
    symbols: list[JavaSymbol],
    edges: list[JavaCallEdge],
    changed_ranges: Iterable[ChangedLineRange],
    changed_files: Iterable[str],
    is_source: Callable[[str], bool],
    parse_errors: list[str],
    omitted_sources: int,
    parse_ms: float,
    has_sources: bool,
) -> JavaAnalysis:
    symbols_by_id = {symbol.symbol_id: symbol for symbol in symbols}
    changed_file_set = {path for path in changed_files if is_source(path)}
    by_path: dict[str, list[ChangedLineRange]] = defaultdict(list)
    for changed_range in changed_ranges:
        by_path[changed_range.file_path].append(changed_range)
    symbols_by_path: dict[str, list[JavaSymbol]] = defaultdict(list)
    for symbol in symbols:
        symbols_by_path[symbol.file_path].append(symbol)

    mapping_started = time.perf_counter()
    changed_ids: set[str] = set()
    file_level: set[str] = set()
    for path in sorted(changed_file_set):
        ranges = by_path.get(path, [])
        matched = {
            symbol.symbol_id
            for symbol in symbols_by_path.get(path, [])
            if any(_intersects(symbol.start_line, symbol.end_line, item) for item in ranges)
        }
        # Prefer the innermost symbols: drop a class when one of its methods changed.
        matched = {
            symbol_id for symbol_id in matched
            if not any(
                other != symbol_id
                and symbols_by_id[other].owner_name == symbols_by_id[symbol_id].qualified_name
                for other in matched
            )
        } or matched
        if matched:
            changed_ids.update(matched)
        else:
            file_level.add(path)
    mapping_ms = (time.perf_counter() - mapping_started) * 1000

    blast_started = time.perf_counter()
    reverse_edges: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        if edge.target_id and edge.target_id != edge.caller_id:
            reverse_edges[edge.target_id].add(edge.caller_id)
    distances: dict[str, int] = {}
    queue: deque[str] = deque()
    for changed_id in sorted(changed_ids):
        for dependent in sorted(reverse_edges.get(changed_id, ())):
            if dependent not in changed_ids and dependent not in distances:
                distances[dependent] = 1
                queue.append(dependent)
    while queue:
        current = queue.popleft()
        for dependent in sorted(reverse_edges.get(current, ())):
            if dependent not in changed_ids and dependent not in distances:
                distances[dependent] = distances[current] + 1
                queue.append(dependent)
    impacted_ids = changed_ids | set(distances)
    affected_symbols = [symbols_by_id[item] for item in impacted_ids if item in symbols_by_id]
    affected_paths = {item.file_path for item in affected_symbols} | changed_file_set
    packages = {item.package_name for item in affected_symbols if item.package_name}
    unresolved = tuple(
        edge for edge in edges
        if edge.resolution != "resolved" and edge.caller_id in impacted_ids
    )
    blast_ms = (time.perf_counter() - blast_started) * 1000

    uncertainties: list[str] = []
    if file_level:
        uncertainties.append(
            f"Some changed {language} files could not be mapped to a function or class "
            "(e.g. module-level code); they remain FILE_LEVEL_CHANGE."
        )
    if unresolved:
        uncertainties.append(
            f"{len(unresolved)} unresolved {language} call relationship(s) affect changed or dependent symbols."
        )
    if parse_errors:
        uncertainties.append(
            f"Tree-sitter reported syntax errors in {len(parse_errors)} {language} source file(s); "
            "their symbol and call graph may be incomplete."
        )
    if omitted_sources:
        uncertainties.append(
            f"{omitted_sources} {language} source file(s) were omitted by the source snapshot limits."
        )
    if not has_sources and changed_file_set:
        uncertainties.append(f"No {language} source snapshot was available for changed {language} files.")

    return JavaAnalysis(
        symbols=tuple(sorted(symbols, key=lambda item: (item.file_path, item.start_line, item.qualified_name))),
        call_edges=tuple(sorted(edges, key=lambda item: (item.caller_id, item.line, item.method_name))),
        changed_symbol_ids=tuple(sorted(changed_ids)),
        file_level_changes=tuple(sorted(file_level)),
        direct_dependents=tuple(sorted(item for item, depth in distances.items() if depth == 1)),
        indirect_dependents=tuple(sorted(item for item, depth in distances.items() if depth > 1)),
        affected_files=tuple(sorted(affected_paths)),
        affected_packages=tuple(sorted(packages)),
        maximum_dependency_depth=max(distances.values()) if distances else None,
        unresolved_relationships=len(unresolved),
        unresolved_calls=unresolved,
        parse_error_files=tuple(sorted(parse_errors)),
        omitted_sources=omitted_sources,
        uncertainties=tuple(uncertainties),
        phase_durations_ms={
            "code_intelligence": round(parse_ms, 3),
            "symbol_mapping": round(mapping_ms, 3),
            "blast_radius": round(blast_ms, 3),
            "call_graph": 0.0,
        },
    )


def merge_analyses(analyses: list[JavaAnalysis]) -> JavaAnalysis:
    """Combine per-language results. Call edges never cross languages, so the union of
    per-language blast radii equals the blast radius of the combined graph."""
    if len(analyses) == 1:
        return analyses[0]
    depths = [item.maximum_dependency_depth for item in analyses if item.maximum_dependency_depth is not None]
    durations: dict[str, float] = defaultdict(float)
    for item in analyses:
        for key, value in item.phase_durations_ms.items():
            durations[key] += value

    def joined(field: str) -> tuple:
        values = [value for item in analyses for value in getattr(item, field)]
        return tuple(sorted(set(values))) if values and isinstance(values[0], str) else tuple(values)

    return JavaAnalysis(
        symbols=joined("symbols"),
        call_edges=joined("call_edges"),
        changed_symbol_ids=joined("changed_symbol_ids"),
        file_level_changes=joined("file_level_changes"),
        direct_dependents=joined("direct_dependents"),
        indirect_dependents=joined("indirect_dependents"),
        affected_files=joined("affected_files"),
        affected_packages=joined("affected_packages"),
        maximum_dependency_depth=max(depths) if depths else None,
        unresolved_relationships=sum(item.unresolved_relationships for item in analyses),
        unresolved_calls=joined("unresolved_calls"),
        parse_error_files=joined("parse_error_files"),
        omitted_sources=sum(item.omitted_sources for item in analyses),
        uncertainties=tuple(dict.fromkeys(value for item in analyses for value in item.uncertainties)),
        phase_durations_ms={key: round(value, 3) for key, value in durations.items()},
    )


def _intersects(start: int, end: int, changed: ChangedLineRange) -> bool:
    return line_range_intersects(start, end, changed)
