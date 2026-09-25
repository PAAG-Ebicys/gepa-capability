"""``gepa setup folders``: where a project keeps the person's experiment templates and own adapters, and what git leaves out.

The folders are visible and apart from the engine folder, so copying a template never copies a credential.
The rules this writes to the project's ``.gitignore`` keep out of git the engine folder (its database and
its encrypted credentials), the templates and the adapters, but not the copies to share
(``<nombre>.compartir/``), which are meant for git. It only appends the rules that are missing, byte for
byte after the person's lines, so running it twice changes nothing; a folder outside the project gets no rule.
"""

from __future__ import annotations

import re
from dataclasses import asdict
from pathlib import Path
from typing import Any

from .config import DEFAULT_FOLDERS, PROJECT_HOME, ConfigError, Settings, project_root, resolved_folders, with_folders
from .templates import SHARE_SUFFIX

GITIGNORE = ".gitignore"
HEADER = "# GEPA: carpeta del motor, plantillas y adaptadores propios (gepa setup folders)"


def gitignore_rules(project: Path, home: Path, folders: dict[str, Path]) -> list[str]:
    """The rules for the engine folder, the templates (all but the copies to share) and the adapters, for those inside the project."""
    def anchored(path: Path) -> str:
        """The folder as a pattern from the project root; a character git reads as a wildcard or an escape matches only itself."""
        return "/" + re.sub(r"([\\*?\[\]])", r"\\\1", path.relative_to(project).as_posix()) + "/"

    rules = []
    if home == project / PROJECT_HOME:
        rules.append(f"{PROJECT_HOME}/")  # the line a person's .gitignore usually has already
    elif home.is_relative_to(project):
        rules.append(anchored(home))
    templates, adapters = folders["templates"], folders["adapters"]
    if templates.is_relative_to(project):  # its content, not the folder: git can re-include a copy to share only inside an included folder
        rules += [f"{anchored(templates)}*", f"!{anchored(templates)}*{SHARE_SUFFIX}/"]
    if adapters.is_relative_to(project):
        rules.append(anchored(adapters))
    return rules


def _append_missing(path: Path, rules: list[str]) -> dict[str, Any]:
    """Append to ``path`` the rules it lacks, after its bytes and with its line breaks; create it when missing."""
    existed = path.exists()
    data = path.read_bytes() if existed else b""
    present = {line.strip() for line in data.decode("utf-8", errors="surrogateescape").splitlines()}
    missing = [rule for rule in rules if rule not in present]
    added = ([] if HEADER in present else [HEADER]) + missing if missing else []
    if added:
        newline = b"\r\n" if b"\r\n" in data else b"\n"
        with open(path, "ab") as handle:
            if data and not data.endswith(b"\n"):
                handle.write(newline)
            handle.write(b"".join(line.encode("utf-8") + newline for line in added))
    return {"path": str(path), "created": not existed, "added": added}


def designate(settings: Settings, *, templates: str | None = None, adapters: str | None = None, cwd: Path | None = None) -> tuple[Settings, dict[str, Any]]:
    """Designate the folders, create them and write the project's ``.gitignore`` rules. A folder not given keeps its designation, or the proposed default.

    Returns the settings to save and the report. Raises :class:`ConfigError` before writing anything if a folder is not acceptable.
    """
    current = asdict(settings.folders) if settings.folders else DEFAULT_FOLDERS
    designated = with_folders(settings, templates=templates or current["templates"], adapters=adapters or current["adapters"], cwd=cwd)
    project = project_root(designated, cwd)
    folders = resolved_folders(designated, cwd)
    try:
        for folder in folders.values():
            folder.mkdir(parents=True, exist_ok=True)
        ignored = _append_missing(project / GITIGNORE, gitignore_rules(project, designated.home, folders))
    except OSError as error:
        raise ConfigError(f"No se pudieron crear las carpetas ni escribir {project / GITIGNORE}: {error.strerror or error}.") from None
    assert designated.folders is not None
    report = {"project": str(project), "folders": {role: str(path) for role, path in folders.items()}, "stored": asdict(designated.folders), "gitignore": ignored}
    return designated, report
