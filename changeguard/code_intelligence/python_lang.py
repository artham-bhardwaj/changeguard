"""Conservative Python symbol and call-graph extraction with tree-sitter."""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterable

import tree_sitter_python
from tree_sitter import Language, Node, Parser

from changeguard.code_intelligence.graph import build_analysis
from changeguard.code_intelligence.java import ChangedLineRange, JavaAnalysis, JavaCallEdge, JavaSymbol

PYTHON_SUFFIXES = (".py", ".pyi")


def is_python_source(path: str) -> bool:
    return path.endswith(PYTHON_SUFFIXES)


def module_name(path: str) -> str:
    """``src/app/services/users.py`` -> ``src.app.services.users``; packages drop ``__init__``."""
    stem = path.rsplit(".", 1)[0]
    parts = [part for part in stem.split("/") if part]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


@dataclass
class _Binding:
    kind: str  # "module" | "symbol"
    file_path: str
    name: str = ""


@dataclass
class PythonFile:
    path: str
    module: str
    tree: object
    source: bytes
    top_level: dict[str, str] = field(default_factory=dict)   # name -> symbol_id
    classes: dict[str, dict[str, str]] = field(default_factory=dict)  # class -> {method: symbol_id}
    bindings: dict[str, _Binding] = field(default_factory=dict)
    star_imports: list[str] = field(default_factory=list)  # file paths


@dataclass
class PythonProject:
    files: dict[str, PythonFile]
    symbols: list[JavaSymbol]
    functions: list[tuple[JavaSymbol, Node, str | None]]  # symbol, node, owning class
    parse_errors: list[str]
    project_method_names: set[str]

    def symbol_at(self, path: str, line: int) -> JavaSymbol | None:
        """Innermost symbol in ``path`` whose definition starts at or contains ``line``."""
        best = None
        for symbol in self.symbols:
            if symbol.file_path == path and symbol.start_line <= line <= symbol.end_line:
                if best is None or symbol.start_line >= best.start_line:
                    best = symbol
        return best


class PythonSymbolMapper:
    language = "Python"

    def __init__(self) -> None:
        self._parser = Parser(Language(tree_sitter_python.language()))

    def parse_project(self, sources: dict[str, str]) -> PythonProject:
        files: dict[str, PythonFile] = {}
        symbols: list[JavaSymbol] = []
        functions: list[tuple[JavaSymbol, Node, str | None]] = []
        parse_errors: list[str] = []
        method_names: set[str] = set()
        for path, text in sorted(sources.items()):
            source = text.encode("utf-8")
            tree = self._parser.parse(source)
            if tree.root_node.has_error:
                parse_errors.append(path)
            info = PythonFile(path, module_name(path), tree, source)
            files[path] = info
            for node in tree.root_node.named_children:
                definition = _unwrap(node)
                if definition is None:
                    continue
                name = _name(definition, source)
                if not name:
                    continue
                if definition.type == "function_definition":
                    symbol = self._symbol(info, name, "function", node, None)
                    info.top_level[name] = symbol.symbol_id
                    symbols.append(symbol)
                    functions.append((symbol, definition, None))
                elif definition.type == "class_definition":
                    class_symbol = self._symbol(info, name, "type", node, None)
                    info.top_level[name] = class_symbol.symbol_id
                    symbols.append(class_symbol)
                    methods: dict[str, str] = {}
                    body = definition.child_by_field_name("body")
                    for member in body.named_children if body else ():
                        method = _unwrap(member)
                        if method is None or method.type != "function_definition":
                            continue
                        method_name = _name(method, source)
                        if not method_name:
                            continue
                        symbol = self._symbol(info, method_name, "method", member, name)
                        methods[method_name] = symbol.symbol_id
                        method_names.add(method_name)
                        symbols.append(symbol)
                        functions.append((symbol, method, name))
                    info.classes[name] = methods
        modules = {info.module: path for path, info in files.items()}
        for info in files.values():
            self._collect_imports(info, modules)
        return PythonProject(files, symbols, functions, parse_errors, method_names)

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
            is_source=is_python_source,
            parse_errors=project.parse_errors,
            omitted_sources=omitted_sources,
            parse_ms=(time.perf_counter() - started) * 1000,
            has_sources=bool(sources),
        )

    def call_edges(self, project: PythonProject) -> list[JavaCallEdge]:
        edges: list[JavaCallEdge] = []
        field_types = self._field_types(project)
        for symbol, node, owner in project.functions:
            info = project.files[symbol.file_path]
            local_types = self._assigned_types(project, info, node, "identifier")
            fields = field_types.get((info.path, owner), {}) if owner else {}
            for call in _descendants(node, "call"):
                function = call.child_by_field_name("function")
                if function is None:
                    continue
                name, target, relevant = self._resolve_call(
                    project, info, function, owner, local_types, fields
                )
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

    def _field_types(self, project: PythonProject) -> dict[tuple[str, str], dict[str, tuple[str, str]]]:
        """``self.repo = Repo()`` anywhere in a class gives the field ``repo`` type ``Repo``."""
        result: dict[tuple[str, str], dict[str, tuple[str, str]]] = {}
        for symbol, node, owner in project.functions:
            if owner:
                info = project.files[symbol.file_path]
                result.setdefault((info.path, owner), {}).update(
                    self._assigned_types(project, info, node, "attribute")
                )
        return result

    def _assigned_types(
        self, project: PythonProject, info: PythonFile, node: Node, target_type: str
    ) -> dict[str, tuple[str, str]]:
        """Map names (or ``self.x`` attributes) assigned from a project class constructor."""
        types: dict[str, tuple[str, str]] = {}
        for assignment in _descendants(node, "assignment"):
            left = assignment.child_by_field_name("left")
            right = assignment.child_by_field_name("right")
            if left is None or right is None or left.type != target_type or right.type != "call":
                continue
            if target_type == "attribute":
                receiver = left.child_by_field_name("object")
                if receiver is None or _text(receiver, info.source) != "self":
                    continue
                key = _text(left.child_by_field_name("attribute"), info.source)
            else:
                key = _text(left, info.source)
            resolved = self._class_ref(project, info, right.child_by_field_name("function"))
            if key and resolved:
                types[key] = resolved
        return types

    def _class_ref(self, project: PythonProject, info: PythonFile, node: Node | None) -> tuple[str, str] | None:
        if node is None:
            return None
        if node.type == "identifier":
            name = _text(node, info.source)
            if name in info.classes:
                return info.path, name
            binding = info.bindings.get(name)
            if binding and binding.kind == "symbol" and binding.name in project.files[binding.file_path].classes:
                return binding.file_path, binding.name
        elif node.type == "attribute":
            receiver = node.child_by_field_name("object")
            binding = info.bindings.get(_text(receiver, info.source)) if receiver is not None else None
            name = _text(node.child_by_field_name("attribute"), info.source)
            if binding and binding.kind == "module" and name in project.files[binding.file_path].classes:
                return binding.file_path, name
        return None

    def _resolve_call(
        self,
        project: PythonProject,
        info: PythonFile,
        function: Node,
        owner: str | None,
        local_types: dict[str, tuple[str, str]] | None = None,
        fields: dict[str, tuple[str, str]] | None = None,
    ) -> tuple[str, str | None, bool]:
        source = info.source
        if function.type == "identifier":
            name = _text(function, source)
            if name in info.top_level:
                return name, _constructor_or_symbol(project, info.path, name), True
            binding = info.bindings.get(name)
            if binding and binding.kind == "symbol":
                return name, _lookup(project, binding.file_path, binding.name), True
            for star_path in info.star_imports:
                if name in project.files[star_path].top_level:
                    return name, _constructor_or_symbol(project, star_path, name), True
            return name, None, False
        if function.type != "attribute":
            return "", None, False
        attribute = function.child_by_field_name("attribute")
        receiver = function.child_by_field_name("object")
        name = _text(attribute, source)
        if receiver is None or not name:
            return name, None, False
        receiver_text = _text(receiver, source)
        typed = None
        if receiver.type == "identifier" and local_types and receiver_text in local_types:
            typed = local_types[receiver_text]
        elif receiver.type == "attribute" and fields:
            inner = receiver.child_by_field_name("object")
            if inner is not None and _text(inner, source) == "self":
                typed = fields.get(_text(receiver.child_by_field_name("attribute"), source))
        if typed:
            target = project.files[typed[0]].classes[typed[1]].get(name)
            if target:
                return name, target, True
        if receiver.type == "identifier" and receiver_text in {"self", "cls"} and owner:
            target = info.classes.get(owner, {}).get(name)
            if target:
                return name, target, True
            return name, None, name in project.project_method_names
        if receiver.type == "identifier":
            binding = info.bindings.get(receiver_text)
            if binding and binding.kind == "module":
                return name, _lookup(project, binding.file_path, name), True
            class_file, class_name = None, None
            if receiver_text in info.classes:
                class_file, class_name = info.path, receiver_text
            elif binding and binding.kind == "symbol" and binding.name in project.files[binding.file_path].classes:
                class_file, class_name = binding.file_path, binding.name
            if class_file and class_name:
                target = project.files[class_file].classes[class_name].get(name)
                return name, target, target is not None or name in project.project_method_names
        return name, None, name in project.project_method_names and not name.startswith("__")

    def _collect_imports(self, info: PythonFile, modules: dict[str, str]) -> None:
        source = info.source
        package = info.module if info.path.endswith("__init__.py") else info.module.rpartition(".")[0]
        for node in info.tree.root_node.named_children:
            if node.type == "import_statement":
                for item in node.named_children:
                    dotted, alias = _import_name(item, source)
                    path = _find_module(modules, dotted)
                    if path is None:
                        continue
                    if alias:
                        info.bindings[alias] = _Binding("module", path)
                    elif "." not in dotted:
                        info.bindings[dotted] = _Binding("module", path)
            elif node.type == "import_from_statement":
                module_node = node.child_by_field_name("module_name")
                if module_node is None:
                    continue
                base = _absolute_module(_text(module_node, source), package)
                module_path = _find_module(modules, base) if base else None
                if any(child.type == "wildcard_import" for child in node.named_children):
                    if module_path:
                        info.star_imports.append(module_path)
                    continue
                for item in node.children_by_field_name("name"):
                    imported, alias = _import_name(item, source)
                    local = alias or imported
                    submodule = _find_module(modules, f"{base}.{imported}" if base else imported)
                    if submodule:
                        info.bindings[local] = _Binding("module", submodule)
                    elif module_path:
                        info.bindings[local] = _Binding("symbol", module_path, imported)

    def _symbol(self, info: PythonFile, name: str, kind: str, node: Node, owner: str | None) -> JavaSymbol:
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
            package_name=info.module.rpartition(".")[0],
            owner_name=f"{info.module}.{owner}" if owner else None,
        )


def _unwrap(node: Node) -> Node | None:
    if node.type == "decorated_definition":
        return node.child_by_field_name("definition")
    if node.type in {"function_definition", "class_definition"}:
        return node
    return None


def _name(node: Node, source: bytes) -> str:
    return _text(node.child_by_field_name("name"), source)


def _text(node: Node | None, source: bytes) -> str:
    return source[node.start_byte:node.end_byte].decode("utf-8", errors="replace") if node else ""


def _descendants(node: Node, node_type: str):
    stack = list(reversed(node.named_children))
    while stack:
        current = stack.pop()
        if current.type == node_type:
            yield current
        stack.extend(reversed(current.named_children))


def _import_name(node: Node, source: bytes) -> tuple[str, str | None]:
    if node.type == "aliased_import":
        return _text(node.child_by_field_name("name"), source), _text(node.child_by_field_name("alias"), source)
    return _text(node, source), None


def _absolute_module(name: str, package: str) -> str:
    if not name.startswith("."):
        return name
    dots = len(name) - len(name.lstrip("."))
    remainder = name[dots:]
    parts = package.split(".") if package else []
    if dots > 1:
        parts = parts[: len(parts) - (dots - 1)] if dots - 1 <= len(parts) else []
    return ".".join([*parts, remainder] if remainder else parts)


def _find_module(modules: dict[str, str], dotted: str) -> str | None:
    """Resolve an import to a project file, allowing source roots such as ``src/`` or ``backend/``."""
    if not dotted:
        return None
    if dotted in modules:
        return modules[dotted]
    matches = [path for name, path in modules.items() if name.endswith("." + dotted)]
    return matches[0] if len(matches) == 1 else None


def _lookup(project: PythonProject, path: str, name: str) -> str | None:
    return _constructor_or_symbol(project, path, name) if name in project.files[path].top_level else None


def _constructor_or_symbol(project: PythonProject, path: str, name: str) -> str:
    info = project.files[path]
    return info.classes.get(name, {}).get("__init__") or info.top_level[name]


# ----- endpoints ---------------------------------------------------------------------------

_ROUTER_FACTORIES = {"FastAPI", "APIRouter", "Flask", "Blueprint", "Quart", "APIBlueprint"}
_DECORATOR_VERBS = {"get", "post", "put", "patch", "delete", "head", "options"}


def extract_python_endpoints(project: PythonProject, revision: str) -> list:
    from changeguard.code_intelligence.endpoints import join_routes, make_endpoint

    routers = {path: _router_variables(info) for path, info in project.files.items()}
    # child router -> [(mount prefix, parent router, mount prefix replaces the child's own prefix)]
    parents: dict[tuple[str, str], list[tuple[str, tuple[str, str], bool]]] = {}
    for path, info in project.files.items():
        for call in _descendants(info.tree.root_node, "call"):
            function = call.child_by_field_name("function")
            if function is None or function.type != "attribute":
                continue
            verb = _text(function.child_by_field_name("attribute"), info.source)
            receiver = _router_ref(project, routers, info, function.child_by_field_name("object"))
            if verb not in {"include_router", "register_blueprint"} or receiver is None:
                continue
            arguments = call.child_by_field_name("arguments")
            positional = [item for item in arguments.named_children if item.type != "keyword_argument"] if arguments else []
            child = _router_ref(project, routers, info, positional[0]) if positional else None
            if child is None:
                continue
            prefix = _keyword_string(arguments, info.source, "prefix", "url_prefix")
            # Flask's register_blueprint(url_prefix=...) overrides the blueprint's own prefix;
            # FastAPI's include_router(prefix=...) is prepended to it.
            replaces = verb == "register_blueprint" and prefix is not None
            parents.setdefault(child, []).append((prefix or "", receiver, replaces))

    resolved: dict[tuple[str, str], list[str]] = {}

    def prefixes(key: tuple[str, str], depth: int = 0) -> list[str]:
        if key in resolved:
            return resolved[key]
        own = routers[key[0]].get(key[1], "")
        if depth > 5 or key not in parents:
            resolved[key] = [own]
            return resolved[key]
        result = sorted({
            join_routes(parent_prefix, mount, "" if replaces else own)
            for mount, parent, replaces in parents[key]
            for parent_prefix in prefixes(parent, depth + 1)
        })
        resolved[key] = result
        return result

    symbols = {item.symbol_id: item for item in project.symbols}
    endpoints = []
    for path, info in project.files.items():
        for node in info.tree.root_node.named_children:
            if node.type != "decorated_definition":
                continue
            definition = node.child_by_field_name("definition")
            if definition is None or definition.type != "function_definition":
                continue
            symbol = symbols.get(info.top_level.get(_name(definition, info.source), ""))
            for decorator in node.named_children:
                if decorator.type != "decorator" or not decorator.named_children:
                    continue
                call = decorator.named_children[0]
                if call.type != "call":
                    continue
                function = call.child_by_field_name("function")
                if function is None or function.type != "attribute":
                    continue
                router = _router_ref(project, routers, info, function.child_by_field_name("object"))
                verb = _text(function.child_by_field_name("attribute"), info.source)
                if router is None:
                    continue
                arguments = call.child_by_field_name("arguments")
                positional = [item for item in arguments.named_children if item.type != "keyword_argument"] if arguments else []
                route = _python_string(positional[0], info.source) if positional else _keyword_string(arguments, info.source, "path", "rule")
                if route is None:
                    continue
                if verb in _DECORATOR_VERBS:
                    methods = [verb.upper()]
                elif verb in {"route", "api_route"}:
                    methods = _keyword_methods(arguments, info.source) or ["GET"]
                else:
                    continue
                framework = "fastapi" if verb == "api_route" or _router_kind(info, project, router) in {"FastAPI", "APIRouter"} else "flask"
                for prefix in prefixes(router):
                    for method in methods:
                        endpoints.append(make_endpoint(
                            method=method,
                            route=join_routes(prefix, route),
                            framework=framework,
                            symbol=symbol,
                            file_path=path,
                            line=decorator.start_point.row + 1,
                            mapping_lines=(decorator.start_point.row + 1,),
                            revision=revision,
                        ))
    return endpoints


def _router_variables(info: PythonFile) -> dict[str, str]:
    """Top-level ``router = APIRouter(prefix="/x")`` style assignments -> {name: prefix}."""
    routers: dict[str, str] = {}
    for statement in info.tree.root_node.named_children:
        assignment = statement.named_children[0] if statement.type == "expression_statement" and statement.named_children else None
        if assignment is None or assignment.type != "assignment":
            continue
        left, right = assignment.child_by_field_name("left"), assignment.child_by_field_name("right")
        if left is None or right is None or left.type != "identifier" or right.type != "call":
            continue
        factory = _text(right.child_by_field_name("function"), info.source).rsplit(".", 1)[-1]
        if factory in _ROUTER_FACTORIES:
            arguments = right.child_by_field_name("arguments")
            routers[_text(left, info.source)] = _keyword_string(arguments, info.source, "prefix", "url_prefix") or ""
    return routers


def _router_kind(info: PythonFile, project: PythonProject, router: tuple[str, str]) -> str:
    target = project.files[router[0]]
    for statement in target.tree.root_node.named_children:
        text = _text(statement, target.source)
        if text.startswith(f"{router[1]} ") or text.startswith(f"{router[1]}="):
            for factory in _ROUTER_FACTORIES:
                if factory + "(" in text:
                    return factory
    return ""


def _router_ref(project: PythonProject, routers: dict, info: PythonFile, node: Node | None) -> tuple[str, str] | None:
    if node is None:
        return None
    if node.type == "identifier":
        name = _text(node, info.source)
        if name in routers[info.path]:
            return info.path, name
        binding = info.bindings.get(name)
        if binding and binding.kind == "symbol" and binding.name in routers.get(binding.file_path, {}):
            return binding.file_path, binding.name
    elif node.type == "attribute":
        receiver = node.child_by_field_name("object")
        name = _text(node.child_by_field_name("attribute"), info.source)
        binding = info.bindings.get(_text(receiver, info.source)) if receiver is not None else None
        if binding and binding.kind == "module" and name in routers.get(binding.file_path, {}):
            return binding.file_path, name
    return None


def _python_string(node: Node | None, source: bytes) -> str | None:
    if node is None or node.type != "string":
        return None
    if any(child.type == "interpolation" for child in node.named_children):
        return None
    content = [child for child in node.named_children if child.type == "string_content"]
    return "".join(_text(child, source) for child in content)


def _keyword_string(arguments: Node | None, source: bytes, *names: str) -> str | None:
    for item in arguments.named_children if arguments else ():
        if item.type == "keyword_argument" and _text(item.child_by_field_name("name"), source) in names:
            return _python_string(item.child_by_field_name("value"), source)
    return None


def _keyword_methods(arguments: Node | None, source: bytes) -> list[str]:
    for item in arguments.named_children if arguments else ():
        if item.type == "keyword_argument" and _text(item.child_by_field_name("name"), source) == "methods":
            value = item.child_by_field_name("value")
            return [
                method.upper() for method in (
                    _python_string(child, source) for child in (value.named_children if value else ())
                ) if method
            ]
    return []
