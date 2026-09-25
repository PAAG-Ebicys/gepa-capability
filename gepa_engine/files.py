"""A new file with a unique name that fails at once in a folder the system denies writing into.

``tempfile`` takes Windows' "access denied" for "that name exists" and tries other names up to
``os.TMP_MAX`` times (2**31 on Windows): a folder denied by its permissions would hang it.
"""

from __future__ import annotations

from pathlib import Path
from typing import IO, Any
from uuid import uuid4


def open_new(folder: Path, prefix: str, suffix: str = "", *, text: bool = True) -> tuple[IO[Any], Path]:
    """Open ``<prefix><random><suffix>`` in ``folder`` for writing (UTF-8 text by default). Raises :class:`OSError`."""
    path = folder / f"{prefix}{uuid4().hex}{suffix}"
    handle = open(path, "x", encoding="utf-8") if text else open(path, "xb")
    return handle, path
