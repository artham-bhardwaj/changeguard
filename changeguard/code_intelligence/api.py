from __future__ import annotations

import ast
from dataclasses import asdict, dataclass, replace
import re
from typing import Literal
from urllib.parse import urlsplit

import tree_sitter_java
from tree_sitter import Language, Node, Parser

from changeguard.code_intelligence.java import ChangedLineRange, JavaSymbol, TYPE_NODES

HttpMethod = Literal[
    "GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE", "UNKNOWN"
]
RelationshipStatus = Literal["AMBIGUOUS", "UNRESOLVED"]
_HTTP_METHODS = {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS", "TRACE"}
_JAVA_MAPPING_METHODS = {
    "GetMapping": ("GET",),
    "PostMapping": ("POST",),
    "PutMapping": ("PUT",),
    "PatchMapping": ("PATCH",),
    "DeleteMapping": ("DELETE",),
}
_SOURCE_EXTENSIONS = {".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}
_EXCLUDED_DIRECTORIES = {
    ".git", ".next", ".nuxt", "build", "coverage", "dist", "generated",
    "node_modules", "out", "target", "vendor",
}
_TOKEN_PATTERN = re.compile(
    r"""(?P<space>\s+)|(?P<comment>//[^\r\n]*|/\*[\s\S]*?\*/)|"""
    r"""(?P<string>"(?:\\.|[^"\\])*"|'(?:\\.|[^'\\])*'|`(?:\\.|[^`\\])*`)|"""
    r"""(?P<identifier>[A-Za-z_$][\w$]*)|(?P<number>\d+)|(?P<punct>.)"""
)


@dataclass(frozen=True)
class ApiEndpoint:
    endpoint_id: str
    http_method: HttpMethod
    route: str
    declaring_type: str
    declaring_method: str
    source_file: str
    source_line: int
    source_revision: str
    provider_symbol_id: str | None
    declaring_type_symbol_id: str | None
    framework: str
    confidence: float
    mapping_source_lines: tuple[int, ...] = ()
    response_type: str | None = None


@dataclass(frozen=True)
class ApiConsumer:
    consumer_id: str
    source_file: str
    source_language: Literal["javascript", "typescript"]
    function_or_scope: str | None
    http_method: HttpMethod
    route: str | None
    route_prefix: str | None
    source_line: int
    extraction_confidence: float


@dataclass(frozen=True)
class ApiDependencyEdge:
    source_node: str
    target_node: str
    relationship_type: Literal["API_CONSUMER"]
    endpoint_id: str
    endpoint_identity: str
    evidence_refs: tuple[str, ...]
    confidence: float


@dataclass(frozen=True)
class ApiRelationshipIssue:
    consumer_id: str
    status: RelationshipStatus
    reason: str
    candidate_endpoint_ids: tuple[str, ...] = ()
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True)
class ApiAnalysisMetrics:
    endpoints_discovered: int = 0
    frontend_files_analyzed: int = 0
    consumers_discovered: int = 0
    deterministic_matches: int = 0
    ambiguous_matches: int = 0
    unresolved_consumers: int = 0
    api_consumer_edges: int = 0
    omitted_frontend_files: int = 0


@dataclass(frozen=True)
class ApiImpact:
    source_revision: str
    endpoints: tuple[ApiEndpoint, ...]
    consumers: tuple[ApiConsumer, ...]
    edges: tuple[ApiDependencyEdge, ...]
    ambiguous_relationships: tuple[ApiRelationshipIssue, ...]
    unresolved_relationships: tuple[ApiRelationshipIssue, ...]
    changed_endpoint_ids: tuple[str, ...]
    changed_consumer_ids: tuple[str, ...]
    impacted_consumer_ids: tuple[str, ...]
    providers_for_changed_consumers: tuple[str, ...]
    metrics: ApiAnalysisMetrics
    uncertainties: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class _Token:
    value: str
    kind: str
    line: int


@dataclass(frozen=True)
class _EndpointDraft:
    method: HttpMethod
    route: str
    owner: str
    method_line: int
    mapping_source_lines: tuple[int, ...]
    source_file: str
    response_type: str | None


@dataclass(frozen=True)
class _ConsumerDraft:
    path: str
    method: HttpMethod
    route: str | None
    route_prefix: str | None
    line: int
    occurrence: int


class ApiContractMapper:
    """Conservatively extract Spring endpoints and common JS/TS HTTP calls."""

    def __init__(
        self,
        *,
        max_frontend_files: int = 200,
        max_file_bytes: int = 256 * 1024,
        max_total_bytes: int = 8 * 1024 * 1024,
    ) -> None:
        if min(max_frontend_files, max_file_bytes, max_total_bytes) < 1:
            raise ValueError("API source limits must be positive")
        self.max_frontend_files = max_frontend_files
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self._java_parser = Parser(Language(tree_sitter_java.language()))

    def analyze(
        self,
        *,
        java_sources: dict[str, str],
        frontend_sources: dict[str, str],
        symbols: tuple[JavaSymbol, ...],
        revision: str,
        changed_files: tuple[str, ...],
        changed_lines: tuple[ChangedLineRange, ...],
        file_level_changes: tuple[str, ...] = (),
        omitted_frontend_files: int = 0,
    ) -> ApiImpact:
        endpoints = self._extract_endpoints(java_sources, symbols, revision)
        bounded_frontend, bounded_omissions = self._bounded_frontend_sources(frontend_sources)
        consumers = self._extract_consumers(bounded_frontend)
        omitted_frontend_files += bounded_omissions
        edges: list[ApiDependencyEdge] = []
        ambiguous: list[ApiRelationshipIssue] = []
        unresolved: list[ApiRelationshipIssue] = []
        endpoint_by_id = {item.endpoint_id: item for item in endpoints}

        for consumer in consumers:
            consumer_ref = f"api-consumer:{consumer.source_file}:{consumer.source_line}"
            if consumer.route is None or consumer.http_method == "UNKNOWN":
                candidates = self._prefix_candidates(consumer, endpoints)
                unresolved.append(
                    ApiRelationshipIssue(
                        consumer.consumer_id,
                        "UNRESOLVED",
                        "The request method or URL is not statically known.",
                        tuple(item.endpoint_id for item in candidates),
                        (consumer_ref,),
                    )
                )
                continue

            method_candidates = [
                endpoint
                for endpoint in endpoints
                if endpoint.http_method == consumer.http_method
            ]
            exact_matches = [
                endpoint
                for endpoint in method_candidates
                if endpoint.route == consumer.route
            ]
            matches = exact_matches or [
                endpoint
                for endpoint in method_candidates
                if _routes_match(endpoint.route, consumer.route)
            ]
            if len(matches) > 1:
                ambiguous.append(
                    ApiRelationshipIssue(
                        consumer.consumer_id,
                        "AMBIGUOUS",
                        "More than one backend endpoint matches this request; no edge was selected.",
                        tuple(sorted(item.endpoint_id for item in matches)),
                        (consumer_ref,),
                    )
                )
                continue
            if not matches:
                continue
            endpoint = matches[0]
            if endpoint.provider_symbol_id is None:
                unresolved.append(
                    ApiRelationshipIssue(
                        consumer.consumer_id,
                        "UNRESOLVED",
                        "The Spring endpoint was extracted but could not be linked to a Java provider symbol.",
                        (endpoint.endpoint_id,),
                        (consumer_ref, f"api-endpoint:{endpoint.endpoint_id}"),
                    )
                )
                continue
            edges.append(
                ApiDependencyEdge(
                    source_node=endpoint.provider_symbol_id,
                    target_node=consumer.consumer_id,
                    relationship_type="API_CONSUMER",
                    endpoint_id=endpoint.endpoint_id,
                    endpoint_identity=f"{endpoint.http_method} {endpoint.route}",
                    evidence_refs=(
                        f"api-endpoint:{endpoint.source_file}:{endpoint.source_line}",
                        consumer_ref,
                    ),
                    confidence=min(endpoint.confidence, consumer.extraction_confidence),
                )
            )

        changed_set = set(changed_files)
        changed_ranges_by_path: dict[str, list[ChangedLineRange]] = {}
        for item in changed_lines:
            changed_ranges_by_path.setdefault(item.file_path, []).append(item)
        changed_method_lines = {
            (item.file_path, item.start_line, item.end_line, item.symbol_id)
            for item in symbols
            if item.file_path in changed_set
            and any(
                _line_ranges_intersect(item.start_line, item.end_line, changed_range)
                for changed_range in changed_ranges_by_path.get(item.file_path, ())
            )
        }
        changed_symbol_ids = {item[3] for item in changed_method_lines}
        changed_endpoint_ids = {
            endpoint.endpoint_id
            for endpoint in endpoints
            if endpoint.source_file in file_level_changes
            or endpoint.provider_symbol_id in changed_symbol_ids
            or any(
                any(
                    _line_ranges_intersect(line, line, changed_range)
                    for changed_range in changed_ranges_by_path.get(
                        endpoint.source_file, ()
                    )
                )
                for line in endpoint.mapping_source_lines
            )
        }

        changed_consumers = {
            item.consumer_id
            for item in consumers
            if item.source_file in changed_set
            and any(
                _line_ranges_intersect(item.source_line, item.source_line, changed_range)
                for changed_range in changed_ranges_by_path.get(item.source_file, ())
            )
        }
        impacted_consumers = {
            edge.target_node for edge in edges if edge.endpoint_id in changed_endpoint_ids
        }
        provider_ids = {
            edge.source_node for edge in edges if edge.target_node in changed_consumers
        }
        uncertainties: list[str] = []
        if omitted_frontend_files:
            uncertainties.append(
                f"{omitted_frontend_files} frontend source file(s) were omitted by API analysis limits."
            )
        if unresolved:
            uncertainties.append(
                f"{len(unresolved)} frontend request relationship(s) could not be resolved deterministically."
            )
        if ambiguous:
            uncertainties.append(
                f"{len(ambiguous)} frontend request relationship(s) matched multiple endpoints."
            )
        metrics = ApiAnalysisMetrics(
            endpoints_discovered=len(endpoints),
            frontend_files_analyzed=len(bounded_frontend),
            consumers_discovered=len(consumers),
            deterministic_matches=len(edges),
            ambiguous_matches=len(ambiguous),
            unresolved_consumers=len(unresolved),
            api_consumer_edges=len(edges),
            omitted_frontend_files=omitted_frontend_files,
        )
        return ApiImpact(
            source_revision=revision,
            endpoints=tuple(endpoints),
            consumers=tuple(consumers),
            edges=tuple(edges),
            ambiguous_relationships=tuple(ambiguous),
            unresolved_relationships=tuple(unresolved),
            changed_endpoint_ids=tuple(sorted(changed_endpoint_ids)),
            changed_consumer_ids=tuple(sorted(changed_consumers)),
            impacted_consumer_ids=tuple(sorted(impacted_consumers)),
            providers_for_changed_consumers=tuple(sorted(provider_ids)),
            metrics=metrics,
            uncertainties=tuple(uncertainties),
        )

    def extract_endpoints(
        self,
        sources: dict[str, str],
        symbols: tuple[JavaSymbol, ...] = (),
        revision: str = "unknown",
    ) -> tuple[ApiEndpoint, ...]:
        return tuple(self._extract_endpoints(sources, symbols, revision))

    def extract_consumers(self, sources: dict[str, str]) -> tuple[ApiConsumer, ...]:
        return tuple(self._extract_consumers(sources))

    def _extract_endpoints(
        self,
        sources: dict[str, str],
        symbols: tuple[JavaSymbol, ...],
        revision: str,
    ) -> list[ApiEndpoint]:
        symbol_by_position = {
            (item.file_path, item.start_line, item.kind): item for item in symbols
        }
        symbol_by_id = {item.symbol_id: item for item in symbols}
        endpoints: list[ApiEndpoint] = []
        drafts: list[_EndpointDraft] = []

        for file_path, source in sorted(sources.items()):
            raw = source.encode("utf-8")
            tree = self._java_parser.parse(raw)
            if tree.root_node.has_error:
                continue
            package = self._package_name(tree.root_node, raw)

            def walk(
                node: Node,
                owner_parts: tuple[str, ...],
                prefixes: tuple[tuple[str, tuple[int, ...]], ...],
            ) -> None:
                if node.type in TYPE_NODES:
                    name_node = node.child_by_field_name("name")
                    if name_node is None:
                        return
                    owner_parts = (*owner_parts, self._text(name_node, raw))
                    owner = ".".join(filter(None, (package, *owner_parts)))
                    modifiers = next(
                        (item for item in node.named_children if item.type == "modifiers"),
                        None,
                    )
                    class_mapping = self._mapping_annotations(modifiers, raw, class_level=True)
                    if class_mapping:
                        prefix_paths = tuple(
                            (
                                normalize_route(path),
                                source_lines,
                            )
                            for _, paths, source_lines in class_mapping
                            for path in paths
                        )
                    else:
                        prefix_paths = prefixes
                    for child in node.named_children:
                        walk(child, owner_parts, prefix_paths)
                    return

                if node.type == "method_declaration" and owner_parts:
                    modifiers = next(
                        (item for item in node.named_children if item.type == "modifiers"),
                        None,
                    )
                    mappings = self._mapping_annotations(modifiers, raw, class_level=False)
                    if mappings:
                        method_name_node = node.child_by_field_name("name")
                        if method_name_node is not None:
                            method_name = self._text(method_name_node, raw)
                            owner = ".".join(filter(None, (package, *owner_parts)))
                            line = node.start_point.row + 1
                            owner_node = symbol_by_position.get((file_path, _type_start_line(node, raw), "type"))
                            # Fall back to qualified owner lookup for symbols whose parser
                            # start-line convention differs from the annotation node.
                            if owner_node is None:
                                owner_node = next(
                                    (
                                        item for item in symbols
                                        if item.file_path == file_path
                                        and item.kind == "type"
                                        and item.qualified_name == owner
                                    ),
                                    None,
                                )
                            method_symbol = symbol_by_position.get((file_path, line, "method"))
                            if method_symbol is None:
                                method_symbol = next(
                                    (
                                        item for item in symbols
                                        if item.file_path == file_path
                                        and item.kind == "method"
                                        and item.owner_name == owner
                                        and item.qualified_name.rsplit(".", 1)[-1].split("(", 1)[0] == method_name
                                        and item.start_line == line
                                    ),
                                    None,
                                )
                            response_node = node.child_by_field_name("type")
                            response_type = self._text(response_node, raw) if response_node else None
                            for mapping_methods, method_paths, method_source_lines in mappings:
                                effective_methods = mapping_methods
                                if not effective_methods:
                                    effective_methods = ("UNKNOWN",)
                                paths = tuple(
                                    (
                                        normalize_route(f"{prefix}/{path}"),
                                        tuple(dict.fromkeys((*prefix_lines, *method_source_lines))),
                                    )
                                    for prefix, prefix_lines in (prefixes or (("", ()),))
                                    for path in method_paths
                                )
                                for http_method in effective_methods:
                                    for route, source_lines in paths:
                                        provider_id = method_symbol.symbol_id if method_symbol else None
                                        type_id = owner_node.symbol_id if owner_node else None
                                        provider_name = (
                                            method_symbol.qualified_name
                                            if method_symbol
                                            else f"{owner}.{method_name}"
                                        )
                                        endpoint_id = (
                                            f"api:{http_method}:{route}:"
                                            f"{provider_id or f'{file_path}:{line}'}"
                                        )
                                        drafts.append(
                                            _EndpointDraft(
                                                http_method,
                                                route,
                                                owner,
                                                line,
                                                source_lines,
                                                file_path,
                                                response_type,
                                            )
                                        )
                                        # Store the stable provider binding with the draft
                                        # lookup key used below, without retaining tree nodes.
                                        provider_by_draft[(file_path, line, http_method, route)] = (
                                            endpoint_id, provider_id, type_id, provider_name
                                        )
                    return

                for child in node.named_children:
                    walk(child, owner_parts, prefixes)

            provider_by_draft: dict[tuple[str, int, HttpMethod, str], tuple[str, str | None, str | None, str]] = {}
            for child in tree.root_node.named_children:
                walk(child, (), (("", ()),))
            for draft in drafts:
                if draft.source_file != file_path:
                    continue
                binding = provider_by_draft.get(
                    (draft.source_file, draft.method_line, draft.method, draft.route)
                )
                if binding is None:
                    continue
                endpoint_id, provider_id, type_id, provider_name = binding
                # Ensure provider IDs refer to symbols from the same parsed Java snapshot.
                if provider_id not in symbol_by_id:
                    provider_id = None
                if type_id not in symbol_by_id:
                    type_id = None
                endpoints.append(
                    ApiEndpoint(
                        endpoint_id=endpoint_id,
                        http_method=draft.method,
                        route=draft.route,
                        declaring_type=draft.owner,
                        declaring_method=provider_name,
                        source_file=draft.source_file,
                        source_line=draft.method_line,
                        source_revision=revision,
                        provider_symbol_id=provider_id,
                        declaring_type_symbol_id=type_id,
                        framework="spring",
                        confidence=0.98 if provider_id else 0.8,
                        mapping_source_lines=draft.mapping_source_lines,
                        response_type=draft.response_type,
                    )
                )
            drafts = [item for item in drafts if item.source_file != file_path]

        unique_endpoints: dict[str, ApiEndpoint] = {}
        for endpoint in endpoints:
            previous = unique_endpoints.get(endpoint.endpoint_id)
            if previous is None:
                unique_endpoints[endpoint.endpoint_id] = endpoint
            else:
                unique_endpoints[endpoint.endpoint_id] = replace(
                    previous,
                    mapping_source_lines=tuple(
                        sorted(
                            set(previous.mapping_source_lines)
                            | set(endpoint.mapping_source_lines)
                        )
                    ),
                )
        return sorted(
            unique_endpoints.values(),
            key=lambda item: (
                item.http_method, item.route, item.source_file, item.declaring_method
            ),
        )

    def _mapping_annotations(
        self, modifiers: Node | None, source: bytes, *, class_level: bool
    ) -> list[tuple[tuple[HttpMethod, ...], tuple[str, ...], tuple[int, ...]]]:
        if modifiers is None:
            return []
        mappings: list[
            tuple[tuple[HttpMethod, ...], tuple[str, ...], tuple[int, ...]]
        ] = []
        for annotation in modifiers.named_children:
            if annotation.type not in {"annotation", "marker_annotation"}:
                continue
            name_node = annotation.child_by_field_name("name")
            if name_node is None:
                name_node = next(iter(annotation.named_children), None)
            if name_node is None:
                continue
            name = self._text(name_node, source).rsplit(".", 1)[-1]
            if name in _JAVA_MAPPING_METHODS and not class_level:
                defaults = _JAVA_MAPPING_METHODS[name]
            elif name == "RequestMapping":
                defaults = ()
            else:
                continue
            paths, methods = self._annotation_arguments(annotation, source)
            if not paths:
                paths = ("",)
            source_lines = tuple(
                range(annotation.start_point.row + 1, annotation.end_point.row + 2)
            )
            mappings.append((methods or defaults, paths, source_lines))
        return mappings

    def _annotation_arguments(
        self, annotation: Node, source: bytes
    ) -> tuple[tuple[str, ...], tuple[HttpMethod, ...]]:
        args = next(
            (item for item in annotation.named_children if item.type == "annotation_argument_list"),
            None,
        )
        if args is None:
            return (), ()
        paths: list[str] = []
        methods: list[HttpMethod] = []
        for item in args.named_children:
            if item.type == "element_value_pair":
                key_node = item.child_by_field_name("key")
                value_node = item.child_by_field_name("value")
                key = self._text(key_node, source) if key_node else ""
                text = self._text(value_node, source) if value_node else ""
                if key in {"value", "path"}:
                    paths.extend(self._string_values(value_node, source))
                elif key == "method":
                    methods.extend(self._request_methods(text))
            else:
                paths.extend(self._string_values(item, source))
        return tuple(dict.fromkeys(paths)), tuple(dict.fromkeys(methods))

    def _string_values(self, node: Node | None, source: bytes) -> list[str]:
        if node is None:
            return []
        values: list[str] = []

        def visit(item: Node) -> None:
            if item.type == "string_literal":
                text = self._text(item, source)
                try:
                    values.append(ast.literal_eval(text))
                except (ValueError, SyntaxError):
                    return
            else:
                for child in item.named_children:
                    visit(child)

        visit(node)
        return values

    def _request_methods(self, text: str) -> tuple[HttpMethod, ...]:
        values = tuple(
            value for value in re.findall(
                r"\b(?:RequestMethod\.)?(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS|TRACE)\b",
                text,
            )
            if value in _HTTP_METHODS
        )
        return tuple(dict.fromkeys(values)) or ("UNKNOWN",)

    def _package_name(self, root: Node, source: bytes) -> str:
        package = next(
            (child for child in root.named_children if child.type == "package_declaration"),
            None,
        )
        if package is None:
            return ""
        return self._text(package, source).removeprefix("package").removesuffix(";").strip()

    def _text(self, node: Node | None, source: bytes) -> str:
        return "" if node is None else source[node.start_byte : node.end_byte].decode("utf-8", errors="replace")

    def _extract_consumers(self, sources: dict[str, str]) -> list[ApiConsumer]:
        result: list[ApiConsumer] = []
        bounded_sources, _ = self._bounded_frontend_sources(sources)
        for path, source in bounded_sources.items():
            tokens = _tokenize(source)
            constants, objects = _static_constants(tokens)
            wrappers = _fetch_json_wrappers(tokens)
            occurrence = 0
            for index, token in enumerate(tokens):
                call = _request_call(tokens, index)
                if call is not None:
                    method, open_index, close_index, url_index = call
                    arguments = _split_top_level(tokens[open_index + 1 : close_index], ",")
                    if not arguments:
                        continue
                    if _is_forwarded_fetch_call(index, arguments, wrappers):
                        continue
                    method = _fetch_method(arguments, method)
                    if url_index < 0:
                        url_tokens = arguments[0]
                        method = _request_config_method(url_tokens)
                    else:
                        url_tokens = arguments[url_index]
                elif (
                    token.value in wrappers
                    and index + 1 < len(tokens)
                    and tokens[index + 1].value == "("
                    and not (
                        index > 0
                        and tokens[index - 1].value == "function"
                    )
                ):
                    close_index = _matching(tokens, index + 1, "(", ")")
                    if close_index is None:
                        continue
                    arguments = _split_top_level(tokens[index + 2 : close_index], ",")
                    if not arguments:
                        continue
                    method = "GET"
                    url_tokens = arguments[0]
                else:
                    continue
                route, prefix = _static_route(url_tokens, constants, objects)
                if route is None and prefix is None and not url_tokens:
                    continue
                occurrence += 1
                extension = path.rsplit(".", 1)[-1].lower()
                result.append(
                    ApiConsumer(
                        consumer_id=f"api-consumer:{path}:{token.line}:{occurrence}",
                        source_file=path,
                        source_language="typescript" if extension in {"ts", "tsx"} else "javascript",
                        function_or_scope=None,
                        http_method=method,
                        route=route,
                        route_prefix=prefix,
                        source_line=token.line,
                        extraction_confidence=0.95 if route is not None else 0.55,
                    )
                )
        return result

    def _bounded_frontend_sources(
        self, sources: dict[str, str]
    ) -> tuple[dict[str, str], int]:
        selected: dict[str, str] = {}
        total_bytes = 0
        omitted = 0
        for path, source in sorted(sources.items()):
            normalized_parts = {part.casefold() for part in path.split("/")}
            suffix = "." + path.rsplit(".", 1)[-1].casefold() if "." in path else ""
            filename = path.rsplit("/", 1)[-1].casefold()
            if (
                suffix not in _SOURCE_EXTENSIONS
                or normalized_parts & _EXCLUDED_DIRECTORIES
                or ".min." in filename
                or ".bundle." in filename
                or ".chunk." in filename
            ):
                continue
            size = len(source.encode("utf-8"))
            if (
                len(selected) >= self.max_frontend_files
                or size > self.max_file_bytes
                or total_bytes + size > self.max_total_bytes
            ):
                omitted += 1
                continue
            selected[path] = source
            total_bytes += size
        return selected, omitted

    def _prefix_candidates(
        self, consumer: ApiConsumer, endpoints: list[ApiEndpoint]
    ) -> list[ApiEndpoint]:
        prefix = consumer.route_prefix
        if not prefix:
            return []
        return [
            endpoint
            for endpoint in endpoints
            if consumer.http_method in {"UNKNOWN", endpoint.http_method}
            and (endpoint.route == prefix or endpoint.route.startswith(prefix.rstrip("/") + "/"))
        ]


def normalize_route(route: str) -> str:
    """Normalize separators without changing case or route-segment meaning."""
    value = route.strip().replace("\\", "/")
    if "://" in value:
        value = urlsplit(value).path
    value = value.split("#", 1)[0].split("?", 1)[0]
    value = re.sub(r"/+", "/", value)
    if not value.startswith("/"):
        value = "/" + value
    if len(value) > 1:
        value = value.rstrip("/")
    return value or "/"


def route_shape(route: str) -> str:
    return "/".join(
        "{}" if re.fullmatch(r"\{[A-Za-z_$][\w$]*\}", segment) else segment
        for segment in normalize_route(route).split("/")
    )


def _routes_match(endpoint_route: str, consumer_route: str) -> bool:
    endpoint_parts = normalize_route(endpoint_route).strip("/").split("/") if endpoint_route != "/" else []
    consumer_parts = normalize_route(consumer_route).strip("/").split("/") if consumer_route != "/" else []
    if len(endpoint_parts) != len(consumer_parts):
        return False
    for expected, actual in zip(endpoint_parts, consumer_parts):
        expected_variable = re.fullmatch(r"\{[A-Za-z_$][\w$]*\}", expected)
        actual_variable = re.fullmatch(r"\{[A-Za-z_$][\w$]*\}", actual)
        if expected_variable or actual_variable:
            if not expected or not actual:
                return False
        elif expected != actual:
            return False
    return True


def _line_ranges_intersect(start: int, end: int, changed: ChangedLineRange) -> bool:
    if changed.new_count <= 0:
        return False
    changed_end = changed.new_start + changed.new_count - 1
    return start <= changed_end and changed.new_start <= end


def _type_start_line(method: Node, source: bytes) -> int:
    parent = method.parent
    while parent is not None and parent.type not in TYPE_NODES:
        parent = parent.parent
    return parent.start_point.row + 1 if parent is not None else -1


def _tokenize(source: str) -> list[_Token]:
    tokens: list[_Token] = []
    line = 1
    for match in _TOKEN_PATTERN.finditer(source):
        kind = match.lastgroup or ""
        if kind in {"space", "comment"}:
            line += match.group().count("\n")
            continue
        tokens.append(
            _Token(
                match.group(),
                "string" if kind == "string" else kind,
                line,
            )
        )
        line += match.group().count("\n")
    return tokens


def _decode_js_string(value: str) -> tuple[str | None, bool]:
    quote = value[0]
    body = value[1:-1]
    if quote == "`" and "${" in body:
        return body.split("${", 1)[0], True
    if quote == "`":
        return re.sub(r"\\([\\`])", r"\1", body), False
    try:
        return ast.literal_eval(value), False
    except (ValueError, SyntaxError, UnicodeDecodeError):
        return None, True


def _static_constants(
    tokens: list[_Token],
) -> tuple[dict[str, str], dict[str, dict[str, str]]]:
    constants: dict[str, str] = {}
    objects: dict[str, dict[str, str]] = {}
    declarations: list[tuple[str, list[_Token]]] = []
    for index in range(len(tokens) - 3):
        if tokens[index].value not in {"const", "let", "var"}:
            continue
        name = tokens[index + 1].value
        if tokens[index + 2].value != "=":
            continue
        start = index + 3
        if tokens[start].value == "{":
            end = _matching(tokens, start, "{", "}")
            if end is not None:
                objects[name] = {}
                declarations.append((name, tokens[start : end + 1]))
            continue
        end = _declaration_expression_end(tokens, start)
        expression = tokens[start:end]
        declarations.append((name, expression))
        if len(expression) == 1 and expression[0].kind == "string":
            decoded, dynamic = _decode_js_string(expression[0].value)
            if decoded is not None and not dynamic:
                constants[name] = decoded

    for name, expression in declarations:
        if expression and expression[0].value == "{":
            pairs = _split_top_level(expression[1:-1], ",")
            fields: dict[str, str] = {}
            for pair in pairs:
                if len(pair) < 3 or pair[1].value != ":":
                    continue
                route, prefix = _static_route(pair[2:], constants, objects)
                if route is not None and prefix is None:
                    fields[pair[0].value.strip("'\"`")] = route
            objects[name] = fields
        elif len(expression) > 1 and any(item.value == "+" for item in expression):
            route, prefix = _static_route(expression, constants, objects)
            if route is not None and prefix is None:
                constants[name] = route
    return constants, objects


def _request_call(
    tokens: list[_Token], index: int
) -> tuple[HttpMethod, int, int, int] | None:
    token = tokens[index]
    method: HttpMethod
    open_index: int
    if token.value == "fetch" and index + 1 < len(tokens) and tokens[index + 1].value == "(":
        if index and tokens[index - 1].value in {".", "?."}:
            return None
        method, open_index = "GET", index + 1
    elif (
        token.kind == "identifier"
        and index + 3 < len(tokens)
        and tokens[index + 1].value in {".", "?."}
        and tokens[index + 2].kind == "identifier"
        and tokens[index + 3].value == "("
    ):
        receiver = token.value.casefold()
        if receiver not in {"axios", "client", "http", "api", "request"} and not receiver.endswith(
            ("client", "http", "api", "fetcher")
        ):
            return None
        verb = tokens[index + 2].value.lower()
        if verb not in {
            "get", "post", "put", "patch", "delete", "head", "options", "trace", "request"
        }:
            return None
        method = (
            verb.upper()
            if verb in {"get", "post", "put", "patch", "delete", "head", "options", "trace"}
            else "UNKNOWN"
        )
        open_index = index + 3
    else:
        return None
    close_index = _matching(tokens, open_index, "(", ")")
    if close_index is None:
        return None
    arguments = _split_top_level(tokens[open_index + 1 : close_index], ",")
    if not arguments:
        return None
    url_index = 0
    if method == "UNKNOWN" and arguments[0] and arguments[0][0].value == "{":
        url_index = -1
    return method, open_index, close_index, url_index


def _fetch_json_wrappers(tokens: list[_Token]) -> dict[str, tuple[str, int, int]]:
    wrappers: dict[str, tuple[str, int, int]] = {}
    for index, token in enumerate(tokens):
        if token.value != "function" or index + 3 >= len(tokens):
            continue
        name = tokens[index + 1]
        open_index = index + 2
        if name.kind != "identifier" or tokens[open_index].value != "(":
            continue
        close_params = _matching(tokens, open_index, "(", ")")
        if close_params is None:
            continue
        parameters = _split_top_level(tokens[open_index + 1 : close_params], ",")
        if not parameters or not parameters[0] or parameters[0][0].kind != "identifier":
            continue
        body_open = close_params + 1
        if body_open >= len(tokens) or tokens[body_open].value != "{":
            continue
        body_close = _matching(tokens, body_open, "{", "}")
        if body_close is None:
            continue
        parameter = parameters[0][0].value
        for body_index in range(body_open + 1, body_close - 2):
            if (
                tokens[body_index].value == "fetch"
                and tokens[body_index + 1].value == "("
                and tokens[body_index + 2].value == parameter
            ):
                wrappers[name.value] = (parameter, body_open, body_close)
                break
    return wrappers


def _is_forwarded_fetch_call(
    call_index: int,
    arguments: list[list[_Token]],
    wrappers: dict[str, tuple[str, int, int]],
) -> bool:
    if not arguments or len(arguments[0]) != 1:
        return False
    argument = arguments[0][0].value
    return any(
        parameter == argument and body_start < call_index < body_end
        for parameter, body_start, body_end in wrappers.values()
    )


def _fetch_method(arguments: list[list[_Token]], method: HttpMethod) -> HttpMethod:
    if method != "GET" or len(arguments) < 2:
        return method
    option_tokens = arguments[1]
    for index in range(len(option_tokens) - 2):
        if (
            option_tokens[index].value == "method"
            and option_tokens[index + 1].value == ":"
            and option_tokens[index + 2].kind == "string"
        ):
            value, dynamic = _decode_js_string(option_tokens[index + 2].value)
            if not dynamic and value and value.upper() in _HTTP_METHODS:
                return value.upper()  # type: ignore[return-value]
            return "UNKNOWN"
    return method


def _request_config_method(tokens: list[_Token]) -> HttpMethod:
    for index in range(len(tokens) - 2):
        if tokens[index].value == "method" and tokens[index + 1].value == ":":
            value = tokens[index + 2]
            if value.kind == "string":
                decoded, dynamic = _decode_js_string(value.value)
                if decoded and not dynamic and decoded.upper() in _HTTP_METHODS:
                    return decoded.upper()  # type: ignore[return-value]
            elif value.kind == "identifier" and value.value.upper() in _HTTP_METHODS:
                return value.value.upper()  # type: ignore[return-value]
            return "UNKNOWN"
    return "UNKNOWN"


def _static_route(
    tokens: list[_Token],
    constants: dict[str, str],
    objects: dict[str, dict[str, str]],
) -> tuple[str | None, str | None]:
    if tokens and tokens[0].value == "{" and tokens[-1].value == "}":
        for index in range(len(tokens) - 2):
            if tokens[index].value in {"url", "uri"} and tokens[index + 1].value == ":":
                end = _next_top_level_delimiter(tokens, index + 2, {",", "}"})
                route, prefix = _static_route(tokens[index + 2 : end], constants, objects)
                if route is not None or prefix is not None:
                    method_index = next(
                        (
                            position + 2 for position, token in enumerate(tokens)
                            if token.value == "method" and position + 1 < len(tokens)
                            and tokens[position + 1].value == ":"
                        ),
                        None,
                    )
                    if method_index is not None and method_index < len(tokens):
                        decoded, dynamic = (
                            _decode_js_string(tokens[method_index].value)
                            if tokens[method_index].kind == "string"
                            else (tokens[method_index].value, False)
                        )
                        # The generic request() method remains UNKNOWN unless handled by
                        # the caller's explicit method extraction in a future extension.
                        del decoded, dynamic
                    return route, prefix
        return None, None
    pieces: list[str] = []
    dynamic = False
    i = 0
    while i < len(tokens):
        token = tokens[i]
        if token.value == "+":
            i += 1
            continue
        if token.kind == "string":
            decoded, is_dynamic = _decode_js_string(token.value)
            if decoded is None:
                dynamic = True
            else:
                if not dynamic:
                    pieces.append(decoded)
                dynamic = dynamic or is_dynamic
        elif token.kind == "identifier":
            value = constants.get(token.value)
            if i + 2 < len(tokens) and tokens[i + 1].value == ".":
                value = objects.get(token.value, {}).get(tokens[i + 2].value)
                i += 2
            if value is None:
                dynamic = True
            elif not dynamic:
                pieces.append(value)
        elif token.value not in {"(", ")"}:
            dynamic = True
        i += 1
    if not pieces:
        return None, None
    joined = "".join(pieces)
    route = _url_route(joined)
    if dynamic:
        return None, normalize_route(route) if route.strip("/") else None
    return normalize_route(route), None


def _url_route(value: str) -> str:
    value = value.strip()
    if "://" in value:
        return urlsplit(value).path or "/"
    return value


def _split_top_level(tokens: list[_Token], delimiter: str) -> list[list[_Token]]:
    result: list[list[_Token]] = []
    current: list[_Token] = []
    stack: list[str] = []
    closing = {")": "(", "]": "[", "}": "{"}
    for token in tokens:
        if token.value in {"(", "[", "{"}:
            stack.append(token.value)
        elif token.value in closing:
            if stack and stack[-1] == closing[token.value]:
                stack.pop()
        if token.value == delimiter and not stack:
            result.append(current)
            current = []
        else:
            current.append(token)
    result.append(current)
    return result


def _matching(
    tokens: list[_Token], start: int, opening: str, closing: str
) -> int | None:
    depth = 0
    for index in range(start, len(tokens)):
        if tokens[index].value == opening:
            depth += 1
        elif tokens[index].value == closing:
            depth -= 1
            if depth == 0:
                return index
    return None


def _next_top_level_delimiter(
    tokens: list[_Token], start: int, delimiters: set[str]
) -> int:
    stack: list[str] = []
    closing = {")": "(", "]": "[", "}": "{"}
    for index in range(start, len(tokens)):
        value = tokens[index].value
        if value in delimiters and not stack:
            return index
        if value in {"(", "[", "{"}:
            stack.append(value)
        elif value in closing and stack and stack[-1] == closing[value]:
            stack.pop()
    return len(tokens)


def _declaration_expression_end(tokens: list[_Token], start: int) -> int:
    if start >= len(tokens):
        return len(tokens)
    start_line = tokens[start].line
    stack: list[str] = []
    closing = {")": "(", "]": "[", "}": "{"}
    for index in range(start, len(tokens)):
        token = tokens[index]
        if not stack and (token.value in {";", ","} or token.line > start_line):
            return index
        if token.value in {"(", "[", "{"}:
            stack.append(token.value)
        elif token.value in closing and stack and stack[-1] == closing[token.value]:
            stack.pop()
    return len(tokens)
