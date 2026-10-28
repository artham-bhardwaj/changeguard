"""Project setup: language detection, evidence discovery/sync, CI workflow, and a setup check."""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

EVIDENCE_DIR = ".changeguard/evidence"
# Folder name (inside .changeguard/evidence or docs/) -> evidence type.
EVIDENCE_FOLDERS = {
    "incidents": "incident", "incident": "incident",
    "postmortems": "postmortem", "postmortem": "postmortem", "post-mortems": "postmortem",
    "runbooks": "runbook", "runbook": "runbook", "playbooks": "runbook",
    "decisions": "architecture_decision", "adr": "architecture_decision", "adrs": "architecture_decision",
    "architecture-decisions": "architecture_decision",
    "issues": "issue", "releases": "release_note", "release-notes": "release_note",
}
EVIDENCE_SUFFIXES = (".md", ".markdown", ".txt", ".rst")
MAX_EVIDENCE_FILES = 500
MAX_EVIDENCE_BYTES = 256 * 1024

LANGUAGES = {
    "Python": (".py",),
    "TypeScript": (".ts", ".tsx", ".mts", ".cts"),
    "JavaScript": (".js", ".jsx", ".mjs", ".cjs"),
    "Java": (".java",),
    "Go": (".go",), "Rust": (".rs",), "C#": (".cs",), "PHP": (".php",), "Ruby": (".rb",),
    "Kotlin": (".kt",), "C/C++": (".c", ".cc", ".cpp", ".h", ".hpp"), "Swift": (".swift",),
}
FULLY_SUPPORTED = {"Python", "TypeScript", "JavaScript", "Java"}

WORKFLOW_PATH = ".github/workflows/changeguard.yml"
WORKFLOW_TEMPLATE = """\
# ChangeGuard review gate: reports on every pull request and fails the check when the risk is
# HIGH or above, until a senior developer approves the latest commit.
#
# To make it block merging: Settings -> Branches -> add a rule for your main branch ->
# "Require status checks to pass" -> select "ChangeGuard review gate".
# Optional repository variable CHANGEGUARD_APPROVERS: comma-separated GitHub logins whose approval
# clears the gate (default: any owner, member or collaborator other than the PR author).
# Optional secret GEMINI_API_KEY: adds an AI investigation to the report.
name: ChangeGuard

on:
  pull_request:
    types: [opened, synchronize, reopened, ready_for_review]
  pull_request_review:
    types: [submitted, dismissed]

permissions:
  contents: read
  pull-requests: write

concurrency:
  group: changeguard-${{{{ github.event.pull_request.number }}}}
  cancel-in-progress: true

jobs:
  changeguard:
    name: ChangeGuard review gate
    if: github.event.pull_request.draft == false
    runs-on: ubuntu-latest
    timeout-minutes: 15
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - name: Install ChangeGuard
        run: pip install "changeguard @ git+https://github.com/{source}@{ref}"
      - name: Load evidence from .changeguard/evidence and docs/
        run: changeguard evidence sync .
      - name: Analyze pull request
        env:
          GITHUB_TOKEN: ${{{{ secrets.GITHUB_TOKEN }}}}
          GEMINI_API_KEY: ${{{{ secrets.GEMINI_API_KEY }}}}
          CHANGEGUARD_LLM_PROVIDER: gemini
          CHANGEGUARD_APPROVERS: ${{{{ vars.CHANGEGUARD_APPROVERS }}}}
        run: |
          LLM=""
          if [ -n "$GEMINI_API_KEY" ]; then LLM="--llm"; fi
          set +e
          changeguard pr analyze "${{{{ github.repository }}}}#${{{{ github.event.pull_request.number }}}}" \\
            $LLM --comment --output changeguard-report.md --fail-on {fail_on}
          status=$?
          set -e
          if [ -f changeguard-report.md ]; then cat changeguard-report.md >> "$GITHUB_STEP_SUMMARY"; fi
          # 0 = passed or approved, 4 = waiting for senior review, others = analysis error.
          exit $status
"""
DEFAULT_SOURCE = "artham-bhardwaj/changeguard"


@dataclass
class ProjectProfile:
    path: str
    name: str
    languages: dict[str, int] = field(default_factory=dict)
    frameworks: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def git_root(path: str) -> Path:
    completed = subprocess.run(
        ["git", "-C", path, "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False, timeout=10
    )
    if completed.returncode != 0:
        raise ValueError(f"{path} is not inside a git repository")
    return Path(completed.stdout.strip())


def tracked_files(root: Path) -> list[str]:
    completed = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-z"], capture_output=True, check=False, timeout=30
    )
    return [item for item in completed.stdout.decode("utf-8", "replace").split("\0") if item]


def detect_project(path: str) -> ProjectProfile:
    root = git_root(path)
    files = tracked_files(root)
    profile = ProjectProfile(str(root), root.name)
    for language, suffixes in LANGUAGES.items():
        count = sum(1 for item in files if item.endswith(suffixes) and not item.endswith(".d.ts"))
        if count:
            profile.languages[language] = count
    profile.languages = dict(sorted(profile.languages.items(), key=lambda item: -item[1]))
    frameworks: set[str] = set()
    for manifest in [item for item in files if item.rsplit("/", 1)[-1] == "package.json"][:20]:
        try:
            data = json.loads((root / manifest).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})}
        for package, label in (("next", "Next.js"), ("react", "React"), ("express", "Express"), ("fastify", "Fastify"),
                               ("hono", "Hono"), ("vue", "Vue"), ("@angular/core", "Angular"), ("svelte", "Svelte"),
                               ("vitest", "Vitest"), ("jest", "Jest"), ("typescript", "TypeScript compiler")):
            if package in deps:
                frameworks.add(label)
    python_manifests = [item for item in files if item.rsplit("/", 1)[-1] in {"pyproject.toml", "requirements.txt", "setup.py", "Pipfile"}]
    python_text = " ".join(_read(root / item).lower() for item in python_manifests[:20])
    for package, label in (("fastapi", "FastAPI"), ("flask", "Flask"), ("django", "Django"), ("pytest", "pytest")):
        if re.search(rf"\b{package}\b", python_text):
            frameworks.add(label)
    if any(item.endswith(("pom.xml", "build.gradle", "build.gradle.kts")) for item in files):
        java_text = " ".join(_read(root / item) for item in files if item.endswith(("pom.xml", "build.gradle", "build.gradle.kts")))
        if "spring" in java_text.lower():
            frameworks.add("Spring")
    profile.frameworks = sorted(frameworks)

    unsupported = [language for language in profile.languages if language not in FULLY_SUPPORTED]
    if unsupported:
        profile.notes.append(
            f"{', '.join(unsupported)}: changes are tracked per file and line, but functions, call graphs "
            "and endpoints are not analyzed."
        )
    if "Django" in frameworks:
        profile.notes.append("Django URL routes are not detected yet; Django view functions are still analyzed.")
    if "Python" in profile.languages and "pytest" not in frameworks:
        profile.notes.append("No pytest setup found: --verify only compiles changed Python files.")
    js_packages = [item for item in files if item.rsplit("/", 1)[-1] == "package.json" and "node_modules" not in item]
    missing = [item for item in js_packages[:5] if not (root / item).parent.joinpath("node_modules").is_dir()]
    if missing and ("TypeScript" in profile.languages or "JavaScript" in profile.languages):
        profile.notes.append(
            "Install JS dependencies (npm install) in "
            + ", ".join(str(Path(item).parent) if "/" in item else "the repository root" for item in missing)
            + " so --verify can type-check and test."
        )
    return profile


def discover_evidence(root: Path) -> list[tuple[Path, str]]:
    """Evidence files in .changeguard/evidence/<type>/ and docs/<type>/ folders, plus CHANGELOG."""
    found: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    for base in (root / EVIDENCE_DIR, root / "docs", root / "doc"):
        if not base.is_dir():
            continue
        for folder in sorted(base.rglob("*")):
            if not folder.is_dir() or "node_modules" in folder.parts:
                continue
            kind = EVIDENCE_FOLDERS.get(folder.name.lower())
            if kind is None:
                continue
            for item in sorted(folder.iterdir()):
                if item.is_file() and item.suffix.lower() in EVIDENCE_SUFFIXES and item not in seen:
                    if item.name.lower() == "readme.md" and folder.parent == root / EVIDENCE_DIR:
                        continue
                    seen.add(item)
                    found.append((item, kind))
    for name in ("CHANGELOG.md", "CHANGES.md", "RELEASE_NOTES.md"):
        candidate = root / name
        if candidate.is_file() and candidate not in seen:
            found.append((candidate, "release_note"))
    return found[:MAX_EVIDENCE_FILES]


def sync_evidence(store, root: Path, repository: str) -> tuple[int, int, list[str]]:
    """Ingest discovered evidence; returns (added, already stored, skipped messages)."""
    from changeguard.evidence.ingest import EvidenceIngestor
    from changeguard.evidence.models import EvidenceDocument

    ingestor = EvidenceIngestor(store)
    added = existing = 0
    skipped: list[str] = []
    for path, kind in discover_evidence(root):
        if path.stat().st_size > MAX_EVIDENCE_BYTES:
            skipped.append(f"{path.relative_to(root)} (larger than {MAX_EVIDENCE_BYTES // 1024} KiB)")
            continue
        content = _read(path)
        if not content.strip():
            continue
        title = next((line[2:].strip() for line in content.splitlines() if line.startswith("# ")), None)
        title = title or path.stem.replace("-", " ").replace("_", " ")
        _, duplicate = ingestor.ingest(
            EvidenceDocument(title, kind, str(path.relative_to(root)), repository, content)  # type: ignore[arg-type]
        )
        if duplicate:
            existing += 1
        else:
            added += 1
    return added, existing, skipped


def create_evidence_folders(root: Path) -> list[str]:
    created = []
    base = root / EVIDENCE_DIR
    for folder in ("incidents", "decisions", "runbooks"):
        target = base / folder
        if not target.exists():
            target.mkdir(parents=True)
            (target / ".gitkeep").write_text("", encoding="utf-8")
            created.append(str(target.relative_to(root)))
    readme = base / "README.md"
    if not readme.exists():
        readme.write_text(
            "# ChangeGuard evidence\n\n"
            "Markdown files here are used as historical evidence when ChangeGuard analyzes changes.\n"
            "Matching past incidents raise the risk score and are listed in reports.\n\n"
            "- `incidents/` or `postmortems/`: what broke, where, and why\n"
            "- `decisions/` (ADRs): why the architecture is the way it is\n"
            "- `runbooks/`: how to operate and recover services\n\n"
            "Run `changeguard evidence sync` after adding files (CI does this automatically).\n",
            encoding="utf-8",
        )
        created.append(str(readme.relative_to(root)))
    return created


def write_github_action(
    root: Path, *, ref: str = "main", source: str = DEFAULT_SOURCE, fail_on: str = "high", force: bool = False
) -> tuple[Path, bool]:
    target = root / WORKFLOW_PATH
    if target.exists() and not force:
        return target, False
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(WORKFLOW_TEMPLATE.format(ref=ref, source=source, fail_on=fail_on.lower()), encoding="utf-8")
    return target, True


@dataclass
class Check:
    name: str
    ok: bool
    detail: str
    optional: bool = False


def doctor() -> list[Check]:
    from changeguard.llm import default_llm_provider
    from changeguard.settings import default_database_path, github_token

    checks: list[Check] = []
    try:
        provider = default_llm_provider()
        if provider == "gemini":
            ok, detail = gemini_key_status()
            checks.append(Check("AI provider", ok, detail, True))
        else:
            base_url = os.getenv("CHANGEGUARD_OLLAMA_BASE_URL", "http://127.0.0.1:11434")
            ok = _reachable(base_url.rstrip("/") + "/api/tags")
            checks.append(Check(
                "AI provider", ok,
                f"Ollama running at {base_url}" if ok
                else f"Ollama not reachable at {base_url}: start Ollama, or switch to Gemini in Settings", True,
            ))
    except ValueError as error:
        checks.append(Check("AI provider", False, str(error), True))
    token = github_token()
    hint = "add a token in Settings or set GITHUB_TOKEN" if os.getenv("CHANGEGUARD_CONTAINER") == "1" else "run `gh auth login`"
    checks.append(Check("GitHub access", bool(token), "token available" if token else f"no token: public repos only ({hint})", True))
    checks.append(Check("git", shutil.which("git") is not None, "installed" if shutil.which("git") else "git is required"))
    usable, detail = sandbox_status()
    checks.append(Check("Sandbox (--verify)", usable, detail, True))
    node = shutil.which("node")
    checks.append(Check("Node.js (JS/TS --verify)", bool(node), "installed" if node else "install Node.js to verify JS/TS projects", True))
    checks.append(Check("Database", True, default_database_path()))
    return checks


_GEMINI_STATUS: dict[str, tuple[float, bool, str]] = {}
_GEMINI_STATUS_SECONDS = 600


def gemini_key_status(*, check=None) -> tuple[bool, str]:
    """Whether the configured Gemini key actually works, and which file it came from."""
    import hashlib

    from changeguard.llm.config import GeminiConfig, env_source
    from changeguard.llm.gemini import check_api_key

    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        return False, "Gemini selected but GEMINI_API_KEY is not set"
    source = env_source("GEMINI_API_KEY")
    where = "" if source in (None, "environment") else f" · key from {source.replace(str(Path.home()), '~')}"
    model = GeminiConfig.from_env().model
    cache_key = hashlib.sha256(f"{model}\0{key}".encode()).hexdigest()
    cached = _GEMINI_STATUS.get(cache_key)
    if cached and check is None and time.monotonic() - cached[0] < _GEMINI_STATUS_SECONDS:
        return cached[1], cached[2] + where
    ok, detail = (check or check_api_key)(key, model)
    _GEMINI_STATUS[cache_key] = (time.monotonic(), ok, detail)
    return ok, detail + where


_SANDBOX_STATUS: tuple[bool, str] | None = None


def sandbox_status() -> tuple[bool, str]:
    """Whether bubblewrap can actually start a sandbox here (cached for the process)."""
    global _SANDBOX_STATUS
    if _SANDBOX_STATUS is not None:
        return _SANDBOX_STATUS
    bwrap = shutil.which("bwrap")
    if not bwrap:
        _SANDBOX_STATUS = (False, "install bubblewrap: sudo apt install bubblewrap")
    else:
        try:
            probe = subprocess.run(
                [bwrap, "--unshare-all", "--ro-bind", "/", "/", "true"], capture_output=True, timeout=10, check=False
            )
            ok = probe.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        if ok:
            _SANDBOX_STATUS = (True, "bubblewrap works")
        elif os.getenv("CHANGEGUARD_CONTAINER") == "1":
            _SANDBOX_STATUS = (False, "not available inside Docker; install ChangeGuard with pip to run tests")
        else:
            _SANDBOX_STATUS = (False, "bubblewrap is installed but cannot create a sandbox (user namespaces are disabled)")
    return _SANDBOX_STATUS


def _reachable(url: str) -> bool:
    from urllib.request import urlopen

    try:
        with urlopen(url, timeout=2):
            return True
    except (OSError, ValueError):
        return False


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
