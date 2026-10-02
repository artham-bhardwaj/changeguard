"""Constrained local filesystem access for repository investigation."""

from __future__ import annotations

from pathlib import Path


class FileTooLargeError(ValueError):
    pass


def get_file(repo_path: str, path: str, *, max_bytes: int = 256 * 1024) -> str:
    root = Path(repo_path).resolve()
    target = (root / path).resolve()
    if not root.is_dir() or root not in target.parents:
        raise ValueError("path must be a file within repo_path")
    if not target.is_file():
        raise FileNotFoundError(path)
    if target.stat().st_size > max_bytes:
        raise FileTooLargeError(f"File exceeds {max_bytes} byte limit: {path}")
    return target.read_text(encoding="utf-8", errors="replace")
