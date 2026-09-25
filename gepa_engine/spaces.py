"""Isolated workspaces: a new folder per evaluation, the files written into it and the observable effects a case leaves there.

A workspace that cannot be set up or read is infrastructure (:class:`~gepa_engine.errors.WorkspaceError`):
it stops the evaluation and is never presented as the candidate's result.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4


def create(root: Path) -> Path:
    """A new empty workspace under ``root``. Raises :class:`OSError`."""
    workspace = root / uuid4().hex[:16]
    workspace.mkdir(parents=True)
    return workspace


def write(root: Path, files: Iterable[tuple[str, str]]) -> None:
    """Write text files by posix path under ``root``, byte for byte (no newline translation). Raises :class:`OSError`."""
    for path, text in files:
        target = root.joinpath(*path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "w", encoding="utf-8", newline="") as handle:
            handle.write(text)


def listing(root: Path) -> dict[str, Path]:
    """Every file of a workspace by its posix path, sorted by that path (the same order on every system)."""
    return dict(sorted((path.relative_to(root).as_posix(), path) for path in root.rglob("*") if path.is_file()))


def digests(root: Path) -> dict[str, str]:
    """The SHA-256 of every file of a workspace: what :func:`effects` compares with afterwards."""
    return {path: hashlib.sha256(item.read_bytes()).hexdigest() for path, item in listing(root).items()}


def effects(root: Path, before: Mapping[str, str], keep: int) -> dict[str, Any]:
    """Files created or modified since ``before``, with their hash and up to ``keep`` characters of content: the observable effects of a case."""
    changed = []
    for path, item in listing(root).items():
        data = item.read_bytes()
        digest = hashlib.sha256(data).hexdigest()
        if before.get(path) == digest:
            continue
        try:
            text: str | None = data.decode("utf-8")
        except UnicodeDecodeError:
            text = None
        changed.append({"path": path, "status": "modified" if path in before else "created", "bytes": len(data), "sha256": digest,
                        "content": None if text is None else _clip(text, keep), "truncated": text is not None and len(text) > keep})
    return {"files": changed}


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"
