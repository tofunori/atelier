"""Dependency-free runtime primitives shared by Atelier entry points."""
from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable

ARTIFACT_EXTENSIONS = frozenset({
    ".png", ".jpg", ".jpeg", ".svg", ".pdf", ".html", ".docx", ".xlsx",
    ".xls", ".csv", ".md", ".py", ".r", ".jl", ".tex", ".sh", ".mp4",
    ".m4v", ".mov", ".webm",
})
EXCLUDED_DIRECTORIES = frozenset({
    ".git", ".fig_thumbs", ".venv", ".venv-era5", ".venv-codex", "node_modules",
    "__pycache__", ".ipynb_checkpoints", "worktrees", ".claude",
    "_gallery_exports", ".prism",
})


def atomic_write_bytes(path: str, data: bytes, mode: int = 0o600) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix="." + os.path.basename(path) + ".",
                               suffix=".tmp", dir=directory)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp, path)
    finally:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def atomic_write_text(path: str, text: str, mode: int = 0o600) -> None:
    atomic_write_bytes(path, text.encode("utf-8"), mode=mode)


def atomic_write_json(path: str, value, mode: int = 0o600) -> None:
    atomic_write_text(path, json.dumps(value, ensure_ascii=False, indent=2) + "\n", mode=mode)


def artifact_snapshot(root: str, extensions: Iterable[str] = ARTIFACT_EXTENSIONS,
                      excluded: Iterable[str] = EXCLUDED_DIRECTORIES) -> dict[str, tuple[int, int]]:
    root = os.path.realpath(root)
    extensions, excluded = set(extensions), set(excluded)
    result = {}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [name for name in dirnames if name not in excluded]
        for name in filenames:
            if name == "figures_index.html":
                continue
            if name != ".atelier-provenance.json" and os.path.splitext(name)[1].lower() not in extensions:
                continue
            full = os.path.join(dirpath, name)
            try:
                stat = os.stat(full)
            except OSError:
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            result[rel] = (int(stat.st_mtime_ns), int(stat.st_size))
    return result
