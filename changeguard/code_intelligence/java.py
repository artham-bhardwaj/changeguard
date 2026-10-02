from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import asdict, dataclass
import time
from typing import Iterable

import tree_sitter_java
from tree_sitter import Language, Node, Parser


@dataclass(frozen=True)
class ChangedLineRange:
    file_path: str
    old_start: int
    old_count: int
    new_start: int
    new_count: int


@dataclass(frozen=True)
class JavaSymbol:
    symbol_id: str
    file_path: str
    qualified_name: str
    kind: str
    start_line: int
    end_line: int
    package_name: str
    owner_name: str | None = None
    arity: int | None = None


@dataclass(frozen=True)
class JavaCallEdge:
    caller_id: str
    method_name: str
    line: int
    target_id: str | None
    resolution: str


@dataclass(frozen=True)
class JavaAnalysis:
    symbols: tuple[JavaSymbol, ...]
    call_edges: tuple[JavaCallEdge, ...]
    changed_symbol_ids: tuple[str, ...]
    file_level_changes: tuple[str, ...]
    direct_dependents: tuple[str, ...]
    indirect_dependents: tuple[str, ...]
    affected_files: tuple[str, ...]
    affected_packages: tuple[str, ...]
    maximum_dependency_depth: int | None
    unresolved_relationships: int
    unresolved_calls: tuple[JavaCallEdge, ...]
    parse_error_files: tuple[str, ...]
    omitted_sources: int
    uncertainties: tuple[str, ...]
    phase_durations_ms: dict[str, float]

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass
class _TypeInfo:
    owner: str
    package: str
    file_path: str
    fields: dict[str, str]


@dataclass
class _MethodInfo:
    symbol: JavaSymbol
    owner: str
    locals: dict[str, str]


@dataclass
class _ParsedType:
    info: _TypeInfo
    node: Node


@dataclass
class _ParsedMethod:
    info: _MethodInfo
    node: Node


@dataclass(frozen=True)
class _CallSite:
    caller: _MethodInfo
    method_name: str
    arity: int
    line: int
    receiver: str
    is_constructor: bool = False


TYPE_NODES = {
    "class_declaration",
    "interface_declaration",
    "enum_declaration",
    "record_declaration",
    "annotation_type_declaration",
}
METHOD_NODES = {"method_declaration", "constructor_declaration"}


class JavaSymbolMapper:
    """Build a bounded, conservative Java method-call graph from one source snapshot."""

    def __init__(self) -> None:
        self._parser = Parser(Language(tree_sitter_java.language()))

    def analyze(
        self,
        sources: dict[str, str],
        changed_ranges: Iterable[ChangedLineRange],
        changed_files: Iterable[str],
        *,
        omitted_sources: int = 0,
    ) -> JavaAnalysis:
        parsed_types: list[_TypeInfo] = []
        parsed_methods: list[_MethodInfo] = []
        call_sites: list[_CallSite] = []
        symbols: list[JavaSymbol] = []
        parse_errors: list[str] = []

        code_started = time.perf_counter()
        for file_path, source in sorted(sources.items()):
            source_bytes = source.encode("utf-8")
            tree = self._parser.parse(source_bytes)
            if tree.root_node.has_error:
                parse_errors.append(file_path)
            package = self._package_name(tree.root_node, source_bytes)
            file_types: list[_ParsedType] = []
            file_methods: list[_ParsedMethod] = []
            self._collect_definitions(
                tree.root_node,
                source_bytes,
                file_path,
                package,
                (),
                file_types,
                file_methods,
                symbols,
            )
            parsed_types.extend(item.info for item in file_types)
            for parsed_method in file_methods:
                parsed_methods.append(parsed_method.info)
                self._collect_calls(
                    parsed_method.node,
                    parsed_method.info,
                    source_bytes,
                    call_sites,
                )
            del file_types, file_methods
            del tree
        code_intelligence_ms = (time.perf_counter() - code_started) * 1000

        symbols_by_id = {symbol.symbol_id: symbol for symbol in symbols}
        changed_ranges = tuple(changed_ranges)
        changed_file_set = set(changed_files)
        changed_ids: set[str] = set()
        file_level: set[str] = set()
        by_path: dict[str, list[ChangedLineRange]] = defaultdict(list)
        for changed_range in changed_ranges:
            by_path[changed_range.file_path].append(changed_range)

        mapping_started = time.perf_counter()
        for path in sorted(changed_file_set):
            if not path.endswith(".java"):
                continue
            path_symbols = [
                symbol for symbol in symbols if symbol.file_path == path
            ]
            ranges = by_path.get(path, [])
            matched = {
                symbol.symbol_id
                for symbol in path_symbols
                if any(
                    self._intersects(symbol.start_line, symbol.end_line, changed_range)
                    for changed_range in ranges
                )
            }
            if matched:
                changed_ids.update(matched)
            else:
                file_level.add(path)
        symbol_mapping_ms = (time.perf_counter() - mapping_started) * 1000

        owner_type = {item.owner: item for item in parsed_types}
        methods_by_owner_name_arity: dict[tuple[str, str, int], list[_MethodInfo]] = defaultdict(list)
        for method in parsed_methods:
            method_name = method.symbol.qualified_name.rsplit(".", 1)[-1].split("(", 1)[0]
            methods_by_owner_name_arity[
                (method.owner, method_name, method.symbol.arity or 0)
            ].append(method)

        graph_started = time.perf_counter()
        edges: list[JavaCallEdge] = []
        for call in call_sites:
            owner = (
                self._owner_for_type(call.method_name, call.caller.symbol.package_name)
                if call.is_constructor
                else self._resolve_receiver(
                    call.receiver, call.caller, owner_type.get(call.caller.owner)
                )
            )
            possible = (
                methods_by_owner_name_arity.get(
                    (owner or "", call.method_name, call.arity), []
                )
                if owner
                else []
            )
            target = possible[0] if len(possible) == 1 else None
            edges.append(
                JavaCallEdge(
                    caller_id=call.caller.symbol.symbol_id,
                    method_name=call.method_name,
                    line=call.line,
                    target_id=target.symbol.symbol_id if target else None,
                    resolution="resolved" if target else "unresolved",
                )
            )
        call_graph_ms = (time.perf_counter() - graph_started) * 1000

        blast_started = time.perf_counter()
        reverse_edges: dict[str, set[str]] = defaultdict(set)
        for edge in edges:
            if edge.target_id:
                reverse_edges[edge.target_id].add(edge.caller_id)
        distances: dict[str, int] = {}
        queue: deque[str] = deque()
        for changed_id in sorted(changed_ids):
            for dependent in sorted(reverse_edges.get(changed_id, ())):
                if dependent not in changed_ids and dependent not in distances:
                    distances[dependent] = 1
                    queue.append(dependent)
        while queue:
            target = queue.popleft()
            for dependent in sorted(reverse_edges.get(target, ())):
                if dependent not in changed_ids and dependent not in distances:
                    distances[dependent] = distances[target] + 1
                    queue.append(dependent)

        direct_ids = tuple(sorted(item for item, depth in distances.items() if depth == 1))
        indirect_ids = tuple(sorted(item for item, depth in distances.items() if depth > 1))
        impacted_ids = changed_ids | set(distances)
        affected_symbols = [symbols_by_id[item] for item in impacted_ids if item in symbols_by_id]
        affected_paths = {item.file_path for item in affected_symbols}
        affected_paths.update(
            path for path in changed_file_set if path.endswith(".java")
        )
        packages = {item.package_name for item in affected_symbols if item.package_name}
        unresolved = tuple(
            edge for edge in edges if edge.resolution != "resolved" and edge.caller_id in impacted_ids
        )
        blast_radius_ms = (time.perf_counter() - blast_started) * 1000

        uncertainties: list[str] = []
        if file_level:
            uncertainties.append(
                "Some changed Java files could not be mapped to a current symbol; "
                "they remain FILE_LEVEL_CHANGE."
            )
        if unresolved:
            uncertainties.append(
                f"{len(unresolved)} unresolved call relationship(s) affect changed or dependent symbols."
            )
        if parse_errors:
            uncertainties.append(
                f"Tree-sitter reported syntax errors in {len(parse_errors)} Java source file(s); "
                "their symbol and call graph may be incomplete."
            )
        if omitted_sources:
            uncertainties.append(
                f"{omitted_sources} Java source file(s) were omitted by the source snapshot limits."
            )
        if not sources and any(path.endswith(".java") for path in changed_file_set):
            uncertainties.append("No Java source snapshot was available for changed Java files.")

        return JavaAnalysis(
            symbols=tuple(sorted(symbols, key=lambda item: (item.file_path, item.start_line, item.qualified_name))),
            call_edges=tuple(sorted(edges, key=lambda item: (item.caller_id, item.line, item.method_name))),
            changed_symbol_ids=tuple(sorted(changed_ids)),
            file_level_changes=tuple(sorted(file_level)),
            direct_dependents=direct_ids,
            indirect_dependents=indirect_ids,
            affected_files=tuple(sorted(affected_paths)),
            affected_packages=tuple(sorted(packages)),
            maximum_dependency_depth=max(distances.values(), default=0) if distances else None,
            unresolved_relationships=len(unresolved),
            unresolved_calls=unresolved,
            parse_error_files=tuple(sorted(parse_errors)),
            omitted_sources=omitted_sources,
            uncertainties=tuple(uncertainties),
            phase_durations_ms={
                "code_intelligence": round(code_intelligence_ms, 3),
                "symbol_mapping": round(symbol_mapping_ms, 3),
                "blast_radius": round(blast_radius_ms, 3),
                "call_graph": round(call_graph_ms, 3),
            },
        )

    def _collect_definitions(
        self,
        node: Node,
        source: bytes,
        file_path: str,
        package: str,
        parent_types: tuple[str, ...],
        types: list[_ParsedType],
        methods: list[_ParsedMethod],
        symbols: list[JavaSymbol],
    ) -> None:
        owner_types = parent_types
        if node.type in TYPE_NODES:
            name_node = node.child_by_field_name("name")
            if name_node is not None:
                simple_name = self._text(name_node, source)
                owner_types = (*parent_types, simple_name)
                owner = ".".join(filter(None, (package, *owner_types)))
                type_symbol = JavaSymbol(
                    symbol_id=f"{file_path}::{owner}",
                    file_path=file_path,
                    qualified_name=owner,
                    kind="type",
                    start_line=node.start_point.row + 1,
                    end_line=node.end_point.row + 1,
                    package_name=package,
                )
                symbols.append(type_symbol)
                fields = self._fields(node, source)
                types.append(_ParsedType(_TypeInfo(owner, package, file_path, fields), node))

        owner = ".".join(filter(None, (package, *owner_types)))
        if node.type in METHOD_NODES and owner_types:
            name_node = node.child_by_field_name("name")
            parameters = node.child_by_field_name("parameters")
            if name_node is not None:
                name = self._text(name_node, source)
                arity = self._arity(parameters)
                parameter_text = self._compact(self._text(parameters, source) if parameters else "")
                qualified = f"{owner}.{name}{parameter_text}"
                method_symbol = JavaSymbol(
                    symbol_id=f"{file_path}::{qualified}",
                    file_path=file_path,
                    qualified_name=qualified,
                    kind="constructor" if node.type == "constructor_declaration" else "method",
                    start_line=node.start_point.row + 1,
                    end_line=node.end_point.row + 1,
                    package_name=package,
                    owner_name=owner,
                    arity=arity,
                )
                symbols.append(method_symbol)
                methods.append(
                    _ParsedMethod(
                        _MethodInfo(method_symbol, owner, self._locals(node, source)), node
                    )
                )
        if node.type == "field_declaration" and owner_types:
            for declarator in node.named_children:
                if declarator.type != "variable_declarator":
                    continue
                name_node = declarator.child_by_field_name("name")
                if name_node is None:
                    continue
                name = self._text(name_node, source)
                qualified = f"{owner}.{name}"
                symbols.append(
                    JavaSymbol(
                        symbol_id=f"{file_path}::{qualified}",
                        file_path=file_path,
                        qualified_name=qualified,
                        kind="field",
                        start_line=declarator.start_point.row + 1,
                        end_line=declarator.end_point.row + 1,
                        package_name=package,
                        owner_name=owner,
                    )
                )
        for child in node.named_children:
            self._collect_definitions(
                child, source, file_path, package, owner_types, types, methods, symbols
            )

    def _collect_calls(
        self,
        method_node: Node,
        method: _MethodInfo,
        source: bytes,
        call_sites: list[_CallSite],
    ) -> None:
        def visit(node: Node) -> None:
            if node is not method_node and node.type in METHOD_NODES:
                return
            if node.type == "method_invocation":
                name_node = node.child_by_field_name("name")
                arguments = node.child_by_field_name("arguments")
                if name_node is not None:
                    receiver = node.child_by_field_name("object")
                    call_sites.append(
                        _CallSite(
                            method,
                            self._text(name_node, source),
                            self._arity(arguments),
                            node.start_point.row + 1,
                            self._text(receiver, source).strip() if receiver else "",
                        )
                    )
            elif node.type == "object_creation_expression":
                type_node = node.child_by_field_name("type")
                if type_node is not None:
                    arguments = node.child_by_field_name("arguments")
                    call_sites.append(
                        _CallSite(
                            method,
                            self._text(type_node, source).rsplit(".", 1)[-1],
                            self._arity(arguments),
                            node.start_point.row + 1,
                            "",
                            is_constructor=True,
                        )
                    )
            for child in node.named_children:
                visit(child)

        visit(method_node)

    def _resolve_receiver(
        self, receiver: str, method: _MethodInfo, type_info: _TypeInfo | None
    ) -> str | None:
        if not receiver:
            return method.owner
        if receiver == "this":
            return method.owner
        if receiver == "super" or type_info is None:
            return None
        simple_receiver = receiver.rsplit(".", 1)[-1]
        if simple_receiver in type_info.fields:
            field_type = type_info.fields[simple_receiver].split("<", 1)[0].strip()
            return self._owner_for_type(field_type, type_info.package)
        if simple_receiver in method.locals:
            local_type = method.locals[simple_receiver].split("<", 1)[0].strip()
            return self._owner_for_type(local_type, type_info.package)
        if receiver == simple_receiver:
            return self._owner_for_type(receiver, type_info.package)
        return None

    def _owner_for_type(self, name: str, package: str) -> str | None:
        simple = name.rsplit(".", 1)[-1]
        return f"{package}.{simple}" if package else simple

    def _fields(self, type_node: Node, source: bytes) -> dict[str, str]:
        fields: dict[str, str] = {}
        body = type_node.child_by_field_name("body")
        if body is None:
            return fields
        for declaration in body.named_children:
            if declaration.type != "field_declaration":
                continue
            field_type = declaration.child_by_field_name("type")
            if field_type is None:
                continue
            type_name = self._compact(self._text(field_type, source))
            for child in declaration.named_children:
                if child.type != "variable_declarator":
                    continue
                name_node = child.child_by_field_name("name")
                if name_node is not None:
                    fields[self._text(name_node, source)] = type_name
        return fields

    def _locals(self, method_node: Node, source: bytes) -> dict[str, str]:
        values: dict[str, str] = {}

        def visit(node: Node) -> None:
            if node.type == "local_variable_declaration":
                local_type = node.child_by_field_name("type")
                if local_type is not None:
                    type_name = self._compact(self._text(local_type, source))
                    for child in node.named_children:
                        if child.type == "variable_declarator":
                            name_node = child.child_by_field_name("name")
                            if name_node is not None:
                                values[self._text(name_node, source)] = type_name
            for child in node.named_children:
                visit(child)

        visit(method_node)
        return values

    def _package_name(self, root: Node, source: bytes) -> str:
        package = next(
            (child for child in root.named_children if child.type == "package_declaration"),
            None,
        )
        if package is None:
            return ""
        text = self._text(package, source)
        return text.removeprefix("package").removesuffix(";").strip()

    def _arity(self, arguments: Node | None) -> int:
        if arguments is None:
            return 0
        return len(arguments.named_children)

    def _intersects(
        self, symbol_start: int, symbol_end: int, changed: ChangedLineRange
    ) -> bool:
        if changed.new_count <= 0:
            return False
        changed_start = changed.new_start
        changed_end = changed.new_start + changed.new_count - 1
        return symbol_start <= changed_end and changed_start <= symbol_end

    def _text(self, node: Node | None, source: bytes) -> str:
        return "" if node is None else source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")

    def _compact(self, value: str) -> str:
        return "".join(value.split())
