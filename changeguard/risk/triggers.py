"""Review triggers: changes a senior developer should always see, whatever their size.

The risk score measures how big and far-reaching a change is. These rules look at what the
change does instead (removed checks, shell commands, secrets, CI files, ...). They read only
the diff and the changed symbol names, so they are fast and deterministic.
"""
from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Literal

TriggerSeverity = Literal["MEDIUM", "HIGH"]

CODE_SUFFIXES = (
    ".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".mts", ".cts", ".java",
    ".go", ".rb", ".php", ".cs", ".kt", ".rs", ".swift",
)
MANIFEST_NAMES = {
    "pyproject.toml", "setup.py", "setup.cfg", "Pipfile", "package.json", "pom.xml",
    "build.gradle", "build.gradle.kts", "go.mod", "Cargo.toml", "Gemfile", "composer.json",
}
WIDE_REACH_CALLERS = 25

# A line that checks something: a condition, an assertion, or an error raised/thrown.
_GUARD = re.compile(r"^\s*(if|elif|unless|assert|guard)\b|\b(raise|throw|abort\s*\()|return\s+(redirect|HttpResponseForbidden|Response)\b.*\b(err|error|status)")
# A line that checks who is allowed to do something.
_ACCESS = re.compile(
    r"(login_required|requires?_auth|require_login|permission|authori[sz]e|is_admin|is_staff|"
    r"csrf|verify_token|check_auth|Depends\([^)]*(auth|user|admin|token)|@PreAuthorize|@Secured|@RolesAllowed)",
    re.IGNORECASE,
)
_SHELL = re.compile(r"\bsubprocess\.|\bos\.system\(|\bos\.popen\(|(?<![\w.])eval\(|(?<![\w.])exec\(|child_process|shell\s*=\s*True|Runtime\.getRuntime\(\)\.exec")
_DESTRUCTIVE = re.compile(
    r"\bDROP\s+(TABLE|DATABASE)\b|\bTRUNCATE\b|\bDELETE\s+FROM\b|shutil\.rmtree|\brm\s+-rf\b|\.unlink\(|os\.remove\(",
    re.IGNORECASE,
)
_SECRET = re.compile(
    r"(api[_-]?key|secret|passw(or)?d|private[_-]?key|access[_-]?token)\w*\s*[:=]\s*[\"'][^\"'\s]{8,}[\"']",
    re.IGNORECASE,
)
_SENSITIVE_WORDS = {
    "auth", "authenticate", "authorize", "login", "logout", "permission", "permissions", "password",
    "passwd", "credential", "credentials", "secret", "token", "payment", "charge", "refund", "send",
    "delete", "destroy", "purge", "drop", "wipe",
}


@dataclass(frozen=True)
class ReviewTrigger:
    kind: str
    severity: TriggerSeverity
    reason: str
    location: str = ""

    def to_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "severity": self.severity, "reason": self.reason, "location": self.location}


def detect_review_triggers(
    diff_text: str,
    changed_files: Iterable[str] = (),
    changed_symbol_names: Iterable[str] = (),
    direct_callers: int | None = None,
    *,
    wide_reach_callers: int = WIDE_REACH_CALLERS,
) -> tuple[ReviewTrigger, ...]:
    triggers: list[ReviewTrigger] = []
    files = list(dict.fromkeys(changed_files))

    for path in files:
        name = path.rsplit("/", 1)[-1]
        if path.startswith(".github/workflows/") or name == "CODEOWNERS" or path.startswith(".changeguard/config"):
            triggers.append(ReviewTrigger(
                "review_process_changed", "HIGH",
                "Changes CI workflows or code-owner rules, which control how changes are reviewed.", path,
            ))
        elif name in MANIFEST_NAMES or re.fullmatch(r"requirements[\w.-]*\.txt", name):
            triggers.append(ReviewTrigger(
                "dependencies_changed", "MEDIUM", "Changes the project's dependencies or build setup.", path,
            ))

    for path, removed, added in _code_hunks(diff_text):
        removed_guards = [line for line in removed if _GUARD.search(line)]
        added_guards = [line for line in added if _GUARD.search(line)]
        is_test = _is_test_path(path)
        if len(removed_guards) > len(added_guards):
            what = "Removes a test assertion or check" if is_test else "Removes a check or error path"
            triggers.append(ReviewTrigger(
                "guard_removed", "HIGH", f"{what}: `{_clip(removed_guards[0])}`", path,
            ))
        removed_access = [line for line in removed if _ACCESS.search(line)]
        if len(removed_access) > sum(1 for line in added if _ACCESS.search(line)):
            triggers.append(ReviewTrigger(
                "access_check_removed", "HIGH",
                f"Removes an access or permission check: `{_clip(removed_access[0])}`", path,
            ))
        if is_test:
            continue  # tests routinely start processes, delete temp files and use fake secrets
        for kind, pattern, reason in (
            ("shell_or_eval", _SHELL, "Runs shell commands or evaluates code"),
            ("destructive_operation", _DESTRUCTIVE, "Deletes data or files"),
            ("hardcoded_secret", _SECRET, "Adds what looks like a hard-coded secret"),
        ):
            hit = next((line for line in added if pattern.search(line)), None)
            if hit is not None:
                shown = "(value hidden)" if kind == "hardcoded_secret" else f"`{_clip(hit)}`"
                triggers.append(ReviewTrigger(kind, "HIGH", f"{reason}: {shown}", path))

    sensitive = [name for name in dict.fromkeys(changed_symbol_names) if _is_sensitive_name(name)]
    if sensitive:
        shown = ", ".join(f"`{name.rsplit('.', 1)[-1]}`" for name in sensitive[:5])
        more = f" and {len(sensitive) - 5} more" if len(sensitive) > 5 else ""
        triggers.append(ReviewTrigger(
            "sensitive_code_changed", "HIGH",
            f"Changes code that handles sending, deleting, auth, secrets or payments: {shown}{more}",
        ))

    if direct_callers is not None and direct_callers >= wide_reach_callers:
        triggers.append(ReviewTrigger(
            "wide_reach", "HIGH", f"Changed code is called from {direct_callers} places.",
        ))
    return _dedupe(triggers)


def _code_hunks(diff_text: str):
    """Yield (path, removed_lines, added_lines) per hunk of code files."""
    path: str | None = None
    removed: list[str] = []
    added: list[str] = []

    def flush():
        if path and path.endswith(CODE_SUFFIXES) and (removed or added):
            return (path, list(removed), list(added))
        return None

    for line in diff_text.splitlines():
        if line.startswith("diff --git ") or line.startswith("@@"):
            item = flush()
            if item:
                yield item
            removed.clear()
            added.clear()
            if line.startswith("diff --git "):
                path = None
        elif line.startswith("+++ "):
            target = line[4:]
            path = target[2:] if target.startswith("b/") else path
        elif line.startswith("--- "):
            source = line[4:]
            if source.startswith("a/"):
                path = source[2:]
        elif line.startswith("-"):
            removed.append(line[1:])
        elif line.startswith("+"):
            added.append(line[1:])
    item = flush()
    if item:
        yield item


def _is_test_path(path: str) -> bool:
    parts = path.lower().split("/")
    name = parts[-1]
    return (
        any(part in {"test", "tests", "__tests__", "spec", "specs"} for part in parts[:-1])
        or name.startswith("test_")
        or re.search(r"(_test|\.test|\.spec|Test|Tests)\.\w+$", path.rsplit("/", 1)[-1]) is not None
    )


def _is_sensitive_name(qualified_name: str) -> bool:
    segments = qualified_name.rstrip("()").replace("/", ".").split(".")
    if any(part in {"test", "tests", "__tests__"} or part.startswith("test_") or part.startswith("Test") for part in segments):
        return False
    name = segments[-1]
    words = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", name).lower().split("_")
    return any(word in _SENSITIVE_WORDS for word in words)


def _clip(line: str, limit: int = 90) -> str:
    text = " ".join(line.split()).replace("`", "'")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _dedupe(triggers: list[ReviewTrigger]) -> tuple[ReviewTrigger, ...]:
    seen: set[tuple[str, str]] = set()
    result = []
    for trigger in triggers:
        key = (trigger.kind, trigger.location)
        if key not in seen:
            seen.add(key)
            result.append(trigger)
    return tuple(result)
