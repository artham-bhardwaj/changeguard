"""Conservative TypeScript/JavaScript symbol, call-graph and route extraction with tree-sitter."""
from __future__ import annotations

import posixpath
import re
import time
from dataclasses import dataclass, field
from typing import Iterable

import tree_sitter_javascript
import tree_sitter_typescript
from tree_sitter import Language, Node, Parser

from changeguard.code_intelligence.api import ApiEndpoint
from changeguard.code_intelligence.endpoints import ROUTE_METHODS, join_routes, make_endpoint
from changeguard.code_intelligence.graph import build_analysis
from changeguard.code_intelligence.java import ChangedLineRange, JavaAnalysis, JavaCallEdge, JavaSymbol

SCRIPT_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
_RESOLVE_SUFFIXES = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
_FUNCTION_VALUES = {"arrow_function", "function_expression", "function", "generator_function"}
_ROUTER_FACTORIES = {"express", "Router", "fastify", "Fastify", "Hono", "Koa", "createRouter"}
_ROUTE_VERBS = {"get", "post", "put", "patch", "delete", "head", "options", "all"}
_NEXT_APP_ROUTE = re.compile(r"(?:^|/)app/(?P<route>(?:.*/)?)route\.(?:ts|js|tsx|jsx|mts|mjs)$")
_NEXT_PAGES_API = re.compile(r"(?:^|/)pages/(?P<route>api/.*)\.(?:ts|js|tsx|jsx)$")


def is_script_source(path: str) -> bool:
    return path.endswith(SCRIPT_SUFFIXES) and not path.endswith(".d.ts")


@dataclass
class _Binding:
    kind: str  # "module" | "symbol" | "default"
    file_path: str
    name: str = ""


@dataclass
class ScriptFile:
    path: str
    module: str
    root: Node
    source: bytes
    top_level: dict[str, str] = field(default_factory=dict)  # name -> symbol id
    classes: dict[str, dict[str, str]] = field(default_factory=dict)
    default_export: str | None = None  # local name of the default export
    bindings: dict[str, _Binding] = field(default_factory=dict)
    routers: dict[str, str] = field(default_factory=dict)  # variable -> own prefix
    trees: list = field(default_factory=list)


@dataclass
class ScriptProject:
    files: dict[str, ScriptFile]
    symbols: list[JavaSymbol]
    functions: list[tuple[JavaSymbol, Node, str | None, ScriptFile]]
    parse_errors: list[str]
    method_names: set[str]
    route_handlers: dict[tuple[str, int], str]  # (file, line of route call) -> synthetic symbol id


class ScriptSymbolMapper:
    language = "TypeScript/JavaScript"

    def __init__(self) -> None:
        self._parsers = {
            "ts": Parser(Language(tree_sitter_typescript.language_typescript())),
            "tsx": Parser(Language(tree_sitter_typescript.language_tsx())),
            "js": Parser(Language(tree_sitter_javascript.language())),
        }

    def parse_project(self, sources: dict[str, str]) -> ScriptProject:
        files: dict[str, ScriptFile] = {}
        symbols: list[JavaSymbol] = []
        functions: list[tuple[JavaSymbol, Node, str | None, ScriptFile]] = []
        parse_errors: list[str] = []
        method_names: set[str] = set()
        route_handlers: dict[tuple[str, int], str] = {}
        for path, text in sorted(sources.items()):
            if not is_script_source(path):
                continue
            source = text.encode("utf-8")
            tree = self._parser_for(path).parse(source)
            if tree.root_node.has_error:
                parse_errors.append(path)
            info = ScriptFile(path, path.rsplit(".", 1)[0], tree.root_node, source, trees=[tree])
            files[path] = info
            for statement in tree.root_node.named_children:
                is_default = statement.type == "export_statement" and _has_token(statement, "default")
                for declaration in _declarations(statement):
                    self._collect_declaration(
                        info, declaration, statement, is_default, symbols, functions, method_names
                    )
                if is_default and statement.child_by_field_name("value") is not None:
                    value = statement.child_by_field_name("value")
                    if value.type == "identifier":
                        info.default_export = _text(value, source)
            self._collect_routers(info)
            for handler_symbol, node in self._inline_route_handlers(info):
                symbols.append(handler_symbol)
                functions.append((handler_symbol, node, None, info))
                route_handlers[(path, handler_symbol.start_line)] = handler_symbol.symbol_id
        for info in files.values():
            self._collect_imports(info, files)
        return ScriptProject(files, symbols, functions, parse_errors, method_names, route_handlers)

    def analyze(
        self,
        sources: dict[str, str],
        changed_ranges: Iterable[ChangedLineRange],
        changed_files: Iterable[str],
        *,
        omitted_sources: int = 0,
    ) -> JavaAnalysis:
        started = time.perf_counter()
        project = self.parse_project(sources)
        edges = self.call_edges(project)
        return build_analysis(
            language=self.language,
            symbols=project.symbols,
            edges=edges,
            changed_ranges=changed_ranges,
            changed_files=changed_files,
            is_source=is_script_source,
            parse_errors=project.parse_errors,
            omitted_sources=omitted_sources,
            parse_ms=(time.perf_counter() - started) * 1000,
            has_sources=bool(project.files),
        )

    # ----- symbols -------------------------------------------------------------------------

    def _collect_declaration(self, info, node, statement, is_default, symbols, functions, method_names) -> None:
        source = info.source
        outer = statement if statement.type == "export_statement" else node
        if node.type in {"function_declaration", "generator_function_declaration"}:
            name = _text(node.child_by_field_name("name"), source)
            if name:
                symbol = self._symbol(info, name, "function", outer, None)
                info.top_level[name] = symbol.symbol_id
                symbols.append(symbol)
                functions.append((symbol, node, None, info))
                if is_default:
                    info.default_export = name
        elif node.type in {"class_declaration", "abstract_class_declaration", "class"}:
            name = _text(node.child_by_field_name("name"), source)
            if not name:
                return
            class_symbol = self._symbol(info, name, "type", outer, None)
            info.top_level[name] = class_symbol.symbol_id
            symbols.append(class_symbol)
            if is_default:
                info.default_export = name
            methods: dict[str, str] = {}
            body = node.child_by_field_name("body")
            for member in body.named_children if body else ():
                member_name, member_node = None, None
                if member.type == "method_definition":
                    member_name, member_node = _text(member.child_by_field_name("name"), source), member
                elif member.type in {"public_field_definition", "field_definition"}:
                    value = member.child_by_field_name("value")
                    if value is not None and value.type in _FUNCTION_VALUES:
                        name_node = member.child_by_field_name("name") or member.child_by_field_name("property")
                        member_name, member_node = _text(name_node, source), value
                if not member_name or member_node is None:
                    continue
                symbol = self._symbol(info, member_name, "method", member, name)
                methods[member_name] = symbol.symbol_id
                method_names.add(member_name)
                symbols.append(symbol)
                functions.append((symbol, member_node, name, info))
            info.classes[name] = methods
        elif node.type in {"interface_declaration", "type_alias_declaration", "enum_declaration"}:
            name = _text(node.child_by_field_name("name"), source)
            if name:
                symbol = self._symbol(info, name, "type", outer, None)
                info.top_level.setdefault(name, symbol.symbol_id)
                symbols.append(symbol)
        elif node.type in {"lexical_declaration", "variable_declaration"}:
            for declarator in node.named_children:
                if declarator.type != "variable_declarator":
                    continue
                name_node = declarator.child_by_field_name("name")
                value = declarator.child_by_field_name("value")
                if name_node is None or name_node.type != "identifier" or value is None:
                    continue
                function_node = _function_value(value)
                name = _text(name_node, source)
                if function_node is None:
                    # Top-level constants (config objects, endpoint maps) so their edits map to a symbol.
                    if statement.parent is not None and statement.parent.type == "program":
                        symbol = self._symbol(info, name, "constant", declarator, None)
                        symbols.append(symbol)
                    continue
                symbol = self._symbol(info, name, "function", outer if len(node.named_children) == 1 else declarator, None)
                info.top_level[name] = symbol.symbol_id
                symbols.append(symbol)
                functions.append((symbol, function_node, None, info))

    def _symbol(self, info: ScriptFile, name: str, kind: str, node: Node, owner: str | None) -> JavaSymbol:
        qualified = f"{info.module}.{owner}.{name}" if owner else f"{info.module}.{name}"
        if kind in {"function", "method"}:
            qualified += "()"
        return JavaSymbol(
            symbol_id=f"{info.path}::{qualified}",
            file_path=info.path,
            qualified_name=qualified,
            kind=kind,
            start_line=node.start_point.row + 1,
            end_line=node.end_point.row + 1,
            package_name=posixpath.dirname(info.path),
            owner_name=f"{info.module}.{owner}" if owner else None,
        )

    # ----- imports -------------------------------------------------------------------------

    def _collect_imports(self, info: ScriptFile, files: dict[str, ScriptFile]) -> None:
        source = info.source
        for statement in info.root.named_children:
            if statement.type == "import_statement":
                target = _resolve_module(info.path, _string(statement.child_by_field_name("source"), source), files)
                if target is None:
                    continue
                for clause in statement.named_children:
                    if clause.type != "import_clause":
                        continue
                    for item in clause.named_children:
                        if item.type == "identifier":
                            info.bindings[_text(item, source)] = _Binding("default", target)
                        elif item.type == "namespace_import":
                            for name in item.named_children:
                                info.bindings[_text(name, source)] = _Binding("module", target)
                        elif item.type == "named_imports":
                            for specifier in item.named_children:
                                if specifier.type != "import_specifier":
                                    continue
                                imported = _text(specifier.child_by_field_name("name"), source)
                                alias = _text(specifier.child_by_field_name("alias"), source) or imported
                                kind = "default" if imported == "default" else "symbol"
                                info.bindings[alias] = _Binding(kind, target, imported)
            elif statement.type in {"lexical_declaration", "variable_declaration"}:
                # CommonJS: const x = require('./x'); const { a, b: c } = require('./x')
                for declarator in statement.named_children:
                    value = declarator.child_by_field_name("value") if declarator.type == "variable_declarator" else None
                    if value is None or value.type != "call_expression":
                        continue
                    function = value.child_by_field_name("function")
                    if function is None or _text(function, source) != "require":
                        continue
                    arguments = value.child_by_field_name("arguments")
                    first = arguments.named_children[0] if arguments and arguments.named_children else None
                    target = _resolve_module(info.path, _string(first, source), files) if first else None
                    if target is None:
                        continue
                    name_node = declarator.child_by_field_name("name")
                    if name_node.type == "identifier":
                        info.bindings[_text(name_node, source)] = _Binding("module", target)
                    elif name_node.type == "object_pattern":
                        for prop in name_node.named_children:
                            if prop.type == "shorthand_property_identifier_pattern":
                                name = _text(prop, source)
                                info.bindings[name] = _Binding("symbol", target, name)
                            elif prop.type == "pair_pattern":
                                key = _text(prop.child_by_field_name("key"), source)
                                local = _text(prop.child_by_field_name("value"), source)
                                info.bindings[local] = _Binding("symbol", target, key)

    # ----- calls ---------------------------------------------------------------------------

    def call_edges(self, project: ScriptProject) -> list[JavaCallEdge]:
        edges: list[JavaCallEdge] = []
        file_types = {path: self._constructed_types(project, info, info.root, top_only=True)
                      for path, info in project.files.items()}
        for symbol, node, owner, info in project.functions:
            types = {**file_types[info.path], **self._constructed_types(project, info, node)}
            for call in _walk(node):
                if call.type == "call_expression":
                    callee = call.child_by_field_name("function")
                elif call.type == "new_expression":
                    callee = call.child_by_field_name("constructor")
                elif call.type in {"jsx_opening_element", "jsx_self_closing_element"}:
                    callee = call.child_by_field_name("name")
                    if callee is None or not _text(callee, info.source)[:1].isupper():
                        continue
                else:
                    continue
                if callee is None:
                    continue
                name, target, relevant = self._resolve(project, info, callee, owner, types)
                if not relevant:
                    continue
                edges.append(JavaCallEdge(
                    caller_id=symbol.symbol_id,
                    method_name=name,
                    line=call.start_point.row + 1,
                    target_id=target,
                    resolution="resolved" if target else "unresolved",
                ))
        return edges

    def _constructed_types(self, project, info, node, top_only: bool = False) -> dict[str, tuple[ScriptFile, str]]:
        """``const store = new UserStore()`` -> ``store`` has class ``UserStore``."""
        types: dict[str, tuple[ScriptFile, str]] = {}
        nodes = info.root.named_children if top_only else _walk(node)
        for item in nodes:
            declarators = item.named_children if item.type in {"lexical_declaration", "variable_declaration"} else (
                [item] if item.type == "variable_declarator" and not top_only else []
            )
            for declarator in declarators:
                if declarator.type != "variable_declarator":
                    continue
                name_node = declarator.child_by_field_name("name")
                value = declarator.child_by_field_name("value")
                if name_node is None or name_node.type != "identifier" or value is None:
                    continue
                resolved = self._class_of_new(project, info, value)
                if resolved:
                    types[_text(name_node, info.source)] = resolved
        return types

    def _class_of_new(self, project, info, value) -> tuple[ScriptFile, str] | None:
        if value.type == "await_expression" and value.named_children:
            value = value.named_children[0]
        if value.type != "new_expression":
            return None
        constructor = value.child_by_field_name("constructor")
        if constructor is None or constructor.type != "identifier":
            return None
        name = _text(constructor, info.source)
        if name in info.classes:
            return info, name
        binding = info.bindings.get(name)
        return _binding_class(project, binding) if binding else None

    def _resolve(self, project, info, callee, owner, types=None) -> tuple[str, str | None, bool]:
        source = info.source
        if callee.type == "identifier":
            name = _text(callee, source)
            if name in info.top_level:
                return name, info.top_level[name], True
            binding = info.bindings.get(name)
            if binding:
                return name, _binding_target(project, binding), True
            return name, None, False
        if callee.type != "member_expression":
            return "", None, False
        receiver = callee.child_by_field_name("object")
        name = _text(callee.child_by_field_name("property"), source)
        if receiver is None or not name:
            return name, None, False
        receiver_text = _text(receiver, source)
        typed = self._class_of_new(project, info, receiver) if receiver.type == "new_expression" else (
            types.get(receiver_text) if types and receiver.type == "identifier" else None
        )
        if typed:
            target = typed[0].classes[typed[1]].get(name)
            if target:
                return name, target, True
        if receiver.type == "this" and owner:
            target = info.classes.get(owner, {}).get(name)
            return name, target, target is not None or name in project.method_names
        if receiver.type == "identifier":
            binding = info.bindings.get(receiver_text)
            if binding and binding.kind == "module":
                target_file = project.files[binding.file_path]
                return name, target_file.top_level.get(name), True
            class_file, class_name = None, None
            if receiver_text in info.classes:
                class_file, class_name = info, receiver_text
            elif binding:
                resolved = _binding_class(project, binding)
                if resolved:
                    class_file, class_name = resolved
            if class_file is not None:
                target = class_file.classes[class_name].get(name)
                return name, target, target is not None or name in project.method_names
        return name, None, name in project.method_names and name not in {"constructor", "then", "catch"}

    # ----- routes --------------------------------------------------------------------------

    def _collect_routers(self, info: ScriptFile) -> None:
        source = info.source
        for declarator in _walk(info.root):
            if declarator.type != "variable_declarator":
                continue
            name_node = declarator.child_by_field_name("name")
            value = declarator.child_by_field_name("value")
            if name_node is None or name_node.type != "identifier" or value is None:
                continue
            factory = _factory_name(value, source)
            if factory in _ROUTER_FACTORIES or factory.endswith(".Router"):
                info.routers[_text(name_node, source)] = _prefix_option(value, source)

    def _route_calls(self, info: ScriptFile):
        """Yield (call node, verb, route string, handler node) for ``router.get('/x', handler)``."""
        source = info.source
        for call in _walk(info.root):
            if call.type != "call_expression":
                continue
            function = call.child_by_field_name("function")
            if function is None or function.type != "member_expression":
                continue
            receiver = function.child_by_field_name("object")
            verb = _text(function.child_by_field_name("property"), source)
            if receiver is None or receiver.type != "identifier" or verb not in _ROUTE_VERBS:
                continue
            if _text(receiver, source) not in info.routers:
                continue
            arguments = call.child_by_field_name("arguments")
            args = [item for item in (arguments.named_children if arguments else []) if item.type != "comment"]
            if len(args) < 2:
                continue
            route = _string(args[0], source)
            if route is None:
                continue
            yield call, _text(receiver, source), verb, route, args[-1]

    def _inline_route_handlers(self, info: ScriptFile):
        for call, router, verb, route, handler in self._route_calls(info):
            function_node = _function_value(handler)
            if function_node is None:
                continue
            label = f"{verb.upper()} {route}"
            qualified = f"{info.module}.<{label}>()"
            yield JavaSymbol(
                symbol_id=f"{info.path}::{qualified}",
                file_path=info.path,
                qualified_name=qualified,
                kind="route_handler",
                start_line=call.start_point.row + 1,
                end_line=call.end_point.row + 1,
                package_name=posixpath.dirname(info.path),
                owner_name=None,
            ), function_node

    def extract_endpoints(self, project: ScriptProject, revision: str) -> list[ApiEndpoint]:
        symbols = {item.symbol_id: item for item in project.symbols}
        mounts = self._mount_prefixes(project)
        endpoints: list[ApiEndpoint] = []
        for info in project.files.values():
            for call, router, verb, route, handler in self._route_calls(info):
                symbol_id = project.route_handlers.get((info.path, call.start_point.row + 1))
                if symbol_id is None and handler.type in {"identifier", "member_expression"}:
                    _, symbol_id, _ = self._resolve(project, info, handler, None)
                prefixes = mounts.get((info.path, router), [""])
                methods = ROUTE_METHODS if verb == "all" else (verb.upper(),)
                for prefix in prefixes:
                    for method in methods:
                        endpoints.append(make_endpoint(
                            method="ANY" if verb == "all" else method,
                            route=join_routes(prefix, info.routers.get(router, ""), route),
                            framework="express",
                            symbol=symbols.get(symbol_id) if symbol_id else None,
                            file_path=info.path,
                            line=call.start_point.row + 1,
                            mapping_lines=tuple(range(call.start_point.row + 1, call.end_point.row + 2)),
                            revision=revision,
                            confidence=0.9 if prefix or len(prefixes) == 1 else 0.7,
                        ))
                        if verb == "all":
                            break
            endpoints.extend(self._next_endpoints(info, symbols, revision))
        return endpoints

    def _mount_prefixes(self, project: ScriptProject) -> dict[tuple[str, str], list[str]]:
        """Resolve ``app.use('/api', router)`` mounts (also across files) to full prefixes."""
        parents: dict[tuple[str, str], list[tuple[str, tuple[str, str] | None]]] = {}
        for info in project.files.values():
            source = info.source
            for call in _walk(info.root):
                if call.type != "call_expression":
                    continue
                function = call.child_by_field_name("function")
                if function is None or function.type != "member_expression":
                    continue
                receiver = function.child_by_field_name("object")
                if _text(function.child_by_field_name("property"), source) not in {"use", "route", "register"}:
                    continue
                if receiver is None or _text(receiver, source) not in info.routers:
                    continue
                arguments = call.child_by_field_name("arguments")
                args = arguments.named_children if arguments else []
                if len(args) < 2:
                    continue
                prefix = _string(args[0], source)
                if prefix is None:
                    continue
                for target in args[1:]:
                    child = self._router_ref(project, info, target)
                    if child:
                        parents.setdefault(child, []).append((prefix, (info.path, _text(receiver, source))))
        resolved: dict[tuple[str, str], list[str]] = {}

        def prefixes(key: tuple[str, str], depth: int = 0) -> list[str]:
            if key in resolved:
                return resolved[key]
            if depth > 5 or key not in parents:
                return [""]
            result = []
            for prefix, parent in parents[key]:
                parent_file = project.files[parent[0]]
                parent_own = parent_file.routers.get(parent[1], "")
                for parent_prefix in prefixes(parent, depth + 1):
                    result.append(join_routes(parent_prefix, parent_own, prefix))
            resolved[key] = sorted(set(result))
            return resolved[key]

        return {key: prefixes(key) for key in parents}

    def _router_ref(self, project, info, node) -> tuple[str, str] | None:
        source = info.source
        if node.type == "identifier":
            name = _text(node, source)
            if name in info.routers:
                return info.path, name
            binding = info.bindings.get(name)
            if binding is None:
                return None
            target = project.files[binding.file_path]
            if binding.kind == "default":
                local = target.default_export or _module_exports_name(target)
                return (target.path, local) if local in target.routers else None
            if binding.kind == "symbol" and binding.name in target.routers:
                return target.path, binding.name
            if binding.kind == "module":
                local = _module_exports_name(target)
                return (target.path, local) if local in target.routers else None
        if node.type == "member_expression":
            receiver = node.child_by_field_name("object")
            binding = info.bindings.get(_text(receiver, source)) if receiver is not None else None
            name = _text(node.child_by_field_name("property"), source)
            if binding and binding.kind == "module" and name in project.files[binding.file_path].routers:
                return binding.file_path, name
        return None

    def _next_endpoints(self, info: ScriptFile, symbols: dict[str, JavaSymbol], revision: str) -> list[ApiEndpoint]:
        endpoints = []
        app_match = _NEXT_APP_ROUTE.search(info.path)
        if app_match:
            segments = [
                segment for segment in app_match["route"].strip("/").split("/")
                if segment and not (segment.startswith("(") and segment.endswith(")")) and not segment.startswith("@")
            ]
            route = "/" + "/".join(segments)
            for method in ROUTE_METHODS:
                symbol_id = info.top_level.get(method)
                if symbol_id:
                    symbol = symbols[symbol_id]
                    endpoints.append(make_endpoint(
                        method=method, route=route, framework="nextjs", symbol=symbol,
                        file_path=info.path, line=symbol.start_line,
                        mapping_lines=(symbol.start_line,), revision=revision,
                    ))
        pages_match = _NEXT_PAGES_API.search(info.path)
        if pages_match and info.default_export in info.top_level:
            route = pages_match["route"]
            route = route[: -len("/index")] if route.endswith("/index") else route
            symbol = symbols[info.top_level[info.default_export]]
            endpoints.append(make_endpoint(
                method="ANY", route="/" + route, framework="nextjs", symbol=symbol,
                file_path=info.path, line=symbol.start_line,
                mapping_lines=(symbol.start_line,), revision=revision, confidence=0.85,
            ))
        return endpoints

    def _parser_for(self, path: str) -> Parser:
        if path.endswith(".tsx"):
            return self._parsers["tsx"]
        if path.endswith((".ts", ".mts", ".cts")):
            return self._parsers["ts"]
        return self._parsers["js"]


# ----- helpers -------------------------------------------------------------------------------

def _declarations(statement: Node) -> list[Node]:
    if statement.type == "export_statement":
        declaration = statement.child_by_field_name("declaration")
        if declaration is not None:
            return [declaration]
        value = statement.child_by_field_name("value")
        if value is not None and value.type in {"class", "function_expression", "function"}:
            return [value] if value.child_by_field_name("name") is not None else []
        return [child for child in statement.named_children if child.type in {
            "function_declaration", "class_declaration", "abstract_class_declaration",
        }]
    return [statement]


def _function_value(value: Node) -> Node | None:
    if value.type in _FUNCTION_VALUES:
        return value
    if value.type in {"call_expression", "as_expression", "satisfies_expression", "parenthesized_expression"}:
        # memo(() => ...), forwardRef(function ...), (() => ...) as Handler
        for child in value.named_children:
            if child.type == "arguments":
                for argument in child.named_children:
                    if argument.type in _FUNCTION_VALUES:
                        return argument
            elif child.type in _FUNCTION_VALUES:
                return child
    return None


def _walk(node: Node):
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        stack.extend(reversed(current.named_children))


def _text(node: Node | None, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace") if node else ""


def _string(node: Node | None, source: bytes) -> str | None:
    if node is None:
        return None
    if node.type == "string":
        return _text(node, source)[1:-1]
    if node.type == "template_string" and not any(child.type == "template_substitution" for child in node.named_children):
        return _text(node, source)[1:-1]
    return None


def _has_token(node: Node, token: str) -> bool:
    return any(child.type == token for child in node.children)


def _factory_name(value: Node, source: bytes) -> str:
    if value.type == "new_expression":
        return _text(value.child_by_field_name("constructor"), source)
    if value.type == "call_expression":
        return _text(value.child_by_field_name("function"), source)
    if value.type == "await_expression" and value.named_children:
        return _factory_name(value.named_children[0], source)
    return ""


def _prefix_option(value: Node, source: bytes) -> str:
    """``new Router({ prefix: '/api' })`` / ``new Hono().basePath('/api')`` style prefixes."""
    for pair in _walk(value):
        if pair.type == "pair" and _text(pair.child_by_field_name("key"), source).strip("'\"") == "prefix":
            return _string(pair.child_by_field_name("value"), source) or ""
    return ""


def _module_exports_name(info: ScriptFile) -> str | None:
    """``module.exports = router`` -> ``router``."""
    for statement in info.root.named_children:
        if statement.type != "expression_statement" or not statement.named_children:
            continue
        expression = statement.named_children[0]
        if expression.type != "assignment_expression":
            continue
        if _text(expression.child_by_field_name("left"), info.source) in {"module.exports", "exports.default"}:
            right = expression.child_by_field_name("right")
            if right is not None and right.type == "identifier":
                return _text(right, info.source)
    return info.default_export


def _binding_target(project: ScriptProject, binding: _Binding) -> str | None:
    target = project.files[binding.file_path]
    if binding.kind == "default":
        name = target.default_export or _module_exports_name(target)
        return target.top_level.get(name) if name else None
    if binding.kind == "symbol":
        return target.top_level.get(binding.name)
    return None


def _binding_class(project: ScriptProject, binding: _Binding) -> tuple[ScriptFile, str] | None:
    target = project.files[binding.file_path]
    name = binding.name if binding.kind == "symbol" else target.default_export if binding.kind == "default" else None
    return (target, name) if name and name in target.classes else None


def _resolve_module(importer: str, specifier: str | None, files: dict[str, ScriptFile]) -> str | None:
    if not specifier:
        return None
    if specifier.startswith("."):
        bases = [posixpath.normpath(posixpath.join(posixpath.dirname(importer), specifier))]
    elif specifier.startswith(("@/", "~/")):
        rest = specifier[2:]
        # Try the alias root at every ancestor folder (handles monorepo packages), nearest first.
        parts = importer.split("/")[:-1]
        bases = []
        for index in range(len(parts), -1, -1):
            root = "/".join(parts[:index])
            bases.extend(f"{root}/{prefix}{rest}".lstrip("/") for prefix in ("", "src/"))
    else:
        return None
    for base in bases:
        stem = re.sub(r"\.(?:js|jsx|mjs|cjs)$", "", base)
        candidates = [base, *(stem + suffix for suffix in _RESOLVE_SUFFIXES),
                      *(f"{base}/index{suffix}" for suffix in _RESOLVE_SUFFIXES)]
        for candidate in candidates:
            if candidate in files:
                return candidate
    return None
