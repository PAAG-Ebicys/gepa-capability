"""Force UTF-8 on the real standard streams: Windows pipes default to cp1252 and would corrupt or abort JSON with accents."""

from __future__ import annotations

from typing import TextIO


def utf8(stream: TextIO) -> TextIO:
    reconfigure = getattr(stream, "reconfigure", None)
    if reconfigure is not None:
        try:
            reconfigure(encoding="utf-8")
        except (ValueError, OSError):
            pass
    return stream


def is_utf8(text: str) -> bool:
    """Whether ``text`` encodes as UTF-8: a lone UTF-16 surrogate (valid in JSON as ``\\ud800``) does not."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def storable(text: str) -> str:
    """``text`` with every character UTF-8 cannot encode replaced by '?', so a model's answer can be stored and sent on."""
    return text.encode("utf-8", "replace").decode("utf-8")
