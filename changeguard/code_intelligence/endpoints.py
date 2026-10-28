"""Shared helpers for backend endpoints found in Python and JavaScript/TypeScript code."""
from __future__ import annotations

import re

from changeguard.code_intelligence.api import ApiEndpoint, normalize_route
from changeguard.code_intelligence.java import JavaSymbol

ROUTE_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS")


def join_routes(*parts: str) -> str:
    joined = "/".join(part.strip("/") for part in parts if part and part.strip("/"))
    return normalize_route("/" + joined)


def standard_route(route: str) -> str:
    """Rewrite framework path parameters to ``{name}``: Flask ``<int:id>``, Express ``:id``,
    Next.js ``[id]`` / ``[...slug]``."""
    route = re.sub(r"<(?:[^:<>]+:)?([A-Za-z_]\w*)>", r"{\1}", route)
    route = re.sub(r"\{([A-Za-z_]\w*):[^{}]+\}", r"{\1}", route)  # Starlette {id:int}
    route = re.sub(r"(?<=/):([A-Za-z_$][\w$]*)\??", r"{\1}", route)
    route = re.sub(r"\[\[?(?:\.\.\.)?([A-Za-z_$][\w$]*)\]?\]", r"{\1}", route)
    return normalize_route(route)


def make_endpoint(
    *,
    method: str,
    route: str,
    framework: str,
    symbol: JavaSymbol | None,
    file_path: str,
    line: int,
    mapping_lines: tuple[int, ...],
    revision: str,
    confidence: float = 0.9,
) -> ApiEndpoint:
    route = standard_route(route)
    provider = symbol.symbol_id if symbol else None
    return ApiEndpoint(
        endpoint_id=f"api:{method}:{route}:{provider or f'{file_path}:{line}'}",
        http_method=method,  # type: ignore[arg-type]
        route=route,
        declaring_type=symbol.owner_name or symbol.package_name if symbol else file_path,
        declaring_method=symbol.qualified_name if symbol else f"{file_path}:{line}",
        source_file=file_path,
        source_line=line,
        source_revision=revision,
        provider_symbol_id=provider,
        declaring_type_symbol_id=None,
        framework=framework,
        confidence=confidence,
        mapping_source_lines=mapping_lines,
    )
