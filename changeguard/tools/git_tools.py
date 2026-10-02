"""Narrow, non-shell Git operations used by the agent."""

from __future__ import annotations

from pathlib import Path
import subprocess

from changeguard.models.schemas import CommitMetadata, DiffResult, JavaSourceSnapshot, RepoStatus


class GitToolError(RuntimeError):
    pass


def _repository(path: str) -> Path:
    repo = Path(path).resolve()
    if not repo.is_dir() or not (repo / ".git").exists():
        raise GitToolError(f"Not a Git repository: {path}")
    return repo


def _git(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True,
        check=False, timeout=15,
    )
    if completed.returncode:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise GitToolError(message or "Git command failed")
    return completed.stdout


def get_repo_status(repo_path: str) -> RepoStatus:
    repo = _repository(repo_path)
    branch = _git(repo, "branch", "--show-current").strip() or "DETACHED"
    head = _git(repo, "rev-parse", "HEAD").strip()
    modified = [line[3:] for line in _git(repo, "status", "--porcelain").splitlines()]
    return RepoStatus(branch=branch, head_commit=head, modified_files=modified)


def get_git_diff(repo_path: str, base: str, head: str, *, max_bytes: int = 1_000_000) -> DiffResult:
    repo = _repository(repo_path)
    changed_files = _git(repo, "diff", "--name-only", base, head).splitlines()
    additions = deletions = 0
    for line in _git(repo, "diff", "--numstat", base, head).splitlines():
        added, removed, _ = line.split("\t", 2)
        if added.isdigit():
            additions += int(added)
            deletions += int(removed)
    diff = _git(repo, "diff", "--no-ext-diff", "--unified=3", base, head)
    encoded = diff.encode("utf-8")
    if len(encoded) > max_bytes:
        diff = encoded[:max_bytes].decode("utf-8", errors="ignore") + "\n[diff truncated]"
    return DiffResult(changed_files, additions, deletions, diff)


def get_recent_commits(
    repo_path: str, limit: int = 10, revision: str | None = None
) -> list[CommitMetadata]:
    if limit < 1 or limit > 100:
        raise ValueError("limit must be between 1 and 100")
    repo = _repository(repo_path)
    target = _git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}").strip() if revision else "HEAD"
    output = _git(repo, "log", f"--max-count={limit}", "--format=%H%x1f%an%x1f%aI%x1f%s", target)
    commits: list[CommitMetadata] = []
    for line in output.splitlines():
        commit, author, timestamp, subject = line.split("\x1f", 3)
        commits.append(CommitMetadata(commit, author, timestamp, subject))
    return commits


def get_java_sources(
    repo_path: str,
    revision: str,
    *,
    max_files: int = 500,
    max_file_bytes: int = 512 * 1024,
    max_total_bytes: int = 20 * 1024 * 1024,
) -> JavaSourceSnapshot:
    """Read a bounded Java source snapshot from a commit, not the working tree."""
    if max_files < 1 or max_file_bytes < 1 or max_total_bytes < 1:
        raise ValueError("Java source snapshot limits must be positive")
    repo = _repository(repo_path)
    resolved = _git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}").strip()
    completed = subprocess.run(
        ["git", "ls-tree", "-r", "-l", "-z", resolved],
        cwd=repo,
        capture_output=True,
        check=False,
        timeout=30,
    )
    if completed.returncode:
        message = completed.stderr.decode("utf-8", errors="replace").strip()
        raise GitToolError(message or "Could not list the Git source snapshot")

    excluded = {".git", ".gradle", "build", "node_modules", "out", "target", "vendor"}
    entries: list[tuple[str, str, int, str]] = []
    omitted = 0
    for record in completed.stdout.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            mode, object_type, object_id, raw_size = metadata.decode("ascii").split()
            path = raw_path.decode("utf-8")
            size = int(raw_size)
        except (UnicodeDecodeError, ValueError):
            omitted += 1
            continue
        if not path.endswith(".java"):
            continue
        if any(part in excluded for part in Path(path).parts) or mode not in {"100644", "100755"}:
            omitted += 1
            continue
        if size > max_file_bytes:
            omitted += 1
            continue
        entries.append((path, object_id, size, mode))

    selected: list[tuple[str, str, int]] = []
    total_bytes = 0
    for path, object_id, size, _ in sorted(entries):
        if len(selected) >= max_files or total_bytes + size > max_total_bytes:
            omitted += 1
            continue
        selected.append((path, object_id, size))
        total_bytes += size

    if not selected:
        return JavaSourceSnapshot(resolved, {}, omitted)
    batch = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=repo,
        input="".join(f"{object_id}\n" for _, object_id, _ in selected).encode("ascii"),
        capture_output=True,
        check=False,
        timeout=30,
    )
    if batch.returncode:
        message = batch.stderr.decode("utf-8", errors="replace").strip()
        raise GitToolError(message or "Could not read the Git Java source snapshot")

    sources: dict[str, str] = {}
    output = batch.stdout
    offset = 0
    for path, expected_object_id, expected_size in selected:
        header_end = output.find(b"\n", offset)
        if header_end < 0:
            raise GitToolError("Git returned a malformed Java source snapshot")
        header = output[offset:header_end].decode("ascii", errors="replace").split()
        if len(header) != 3 or header[0] != expected_object_id or header[1] != "blob":
            raise GitToolError(f"Git returned an invalid blob header for {path}")
        try:
            actual_size = int(header[2])
        except ValueError as error:
            raise GitToolError(f"Git returned an invalid blob size for {path}") from error
        data_start = header_end + 1
        data_end = data_start + actual_size
        if actual_size != expected_size or data_end >= len(output) or output[data_end:data_end + 1] != b"\n":
            raise GitToolError(f"Git returned a truncated Java source blob for {path}")
        sources[path] = output[data_start:data_end].decode("utf-8", errors="replace")
        offset = data_end + 1
    return JavaSourceSnapshot(resolved, sources, omitted)


def get_revision_files(
    repo_path: str, revision: str, *, max_files: int = 10_000
) -> tuple[str, ...]:
    if max_files < 1:
        raise ValueError("max_files must be positive")
    repo = _repository(repo_path)
    resolved = _git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}").strip()
    output = _git(repo, "ls-tree", "-r", "--name-only", "-z", resolved)
    files = tuple(
        sorted(
            path
            for path in output.split("\0")
            if path and not path.startswith("/") and ".." not in Path(path).parts
        )
    )
    if len(files) > max_files:
        raise GitToolError(
            f"Revision contains more than the supported {max_files} tracked files"
        )
    return files


def get_revision_file(
    repo_path: str, revision: str, path: str, *, max_bytes: int = 512 * 1024
) -> str:
    if not path or Path(path).is_absolute() or ".." in Path(path).parts or path.startswith("-"):
        raise ValueError("path must be a tracked relative file path")
    if max_bytes < 1:
        raise ValueError("max_bytes must be positive")
    repo = _repository(repo_path)
    resolved = _git(repo, "rev-parse", "--verify", f"{revision}^{{commit}}").strip()
    tree = subprocess.run(
        ["git", "ls-tree", "-z", resolved, "--", path],
        cwd=repo,
        capture_output=True,
        check=False,
        timeout=15,
    )
    if tree.returncode:
        message = tree.stderr.decode("utf-8", errors="replace").strip()
        raise GitToolError(message or "Could not inspect revision file")
    records = [record for record in tree.stdout.split(b"\0") if record]
    if len(records) != 1 or b"\t" not in records[0]:
        raise FileNotFoundError(f"File is not tracked at revision: {path}")
    metadata, returned_path = records[0].split(b"\t", 1)
    try:
        mode, object_type, object_id = metadata.decode("ascii").split()
        actual_path = returned_path.decode("utf-8")
    except (UnicodeDecodeError, ValueError) as error:
        raise GitToolError(f"Git returned malformed metadata for {path}") from error
    if actual_path != path or object_type != "blob" or mode not in {"100644", "100755"}:
        raise ValueError("path must refer to a regular tracked file")
    content = subprocess.run(
        ["git", "cat-file", "blob", object_id],
        cwd=repo,
        capture_output=True,
        check=False,
        timeout=15,
    )
    if content.returncode:
        message = content.stderr.decode("utf-8", errors="replace").strip()
        raise GitToolError(message or f"Could not read revision file: {path}")
    if len(content.stdout) > max_bytes:
        raise ValueError(f"Revision file exceeds {max_bytes} byte limit: {path}")
    return content.stdout.decode("utf-8", errors="replace")
