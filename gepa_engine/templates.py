"""Experiment templates: how a class of tasks prepares its cases, runs and evaluates an artifact, and presents its results.

A template is a folder of the project, and that folder is its source of truth (ADR 0002): a person reads,
edits and copies it. ``template.json`` declares only what a person writes or adjusts: name, description,
version number, artifact type, adapter (the built-in one by name and version, or an agent's adapter whose
files are in the ``adapter/`` subfolder), the executor and evaluator settings of a built-in adapter, the
guide to gather cases, the rules to present results and, as information only, where it came from. What the
adapter decides (its evaluator with the aggregation rule, and the case fields) is deduced each time the
folder is read, so a hand edit never leaves it outdated. A template never holds cases, previews, results,
models or credentials: applying it prepares a new draft with new cases, whose original runs again in the
preview and needs its own approval.

Each content is identified by the SHA-256 of the whole folder, computed as an adapter's. The version number
is for people: two contents that declare the same number are still two templates. Checking reads the folder
without the original and never loads the adapter's program. Applying it checks its settings on the new
original and freezes a copy, and a draft, its approval and its jobs keep that copy whatever the folder holds
afterwards.

A template comes into a project in three ways. GEPA's example templates, which use built-in adapters and bring
no program, are copied into a new folder. A copy to share, ``<nombre>.compartir/``, is written by the host agent
with the sensitive parts replaced by holes: it is left out of the list and never applied. An agent's adapter
brought from another project is not loaded until the person trusts its program with a command they run
themselves, unless this project already froze a copy of it.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .artifacts import KINDS
from .datasets import find_secret_key
from .declaration import AdapterError, Folder, parse_declaration, read_folder, write_folder
from .errors import JobError

FORMAT = "gepa-template-v2"
DECLARATION = "template.json"
ADAPTER_DIR = "adapter"  # an agent's adapter, inside its template: the files of its frozen copy
SHARE_SUFFIX = ".compartir"  # the folder of a copy to share: <nombre>.compartir/, next to its template
EXAMPLES = Path(__file__).resolve().parent / "template_examples"  # example templates `gepa template init` copies: built-in adapters, no program
REQUIRED = ("format", "name", "version", "artifactType", "adapter")
OPTIONAL = ("description", "executor", "evaluator", "cases", "presentation", "origin")
ORIGIN_KEYS = ("datasetId", "draftId", "project")
SAVE_KEYS = frozenset({"from", "name", "description", "cases", "presentation", "path"})
# Common shapes of provider keys and tokens (OpenAI, OpenRouter and Anthropic, GitHub, Slack, AWS, Google, a bearer token): text written in a
# template is refused when it looks like one, besides holding the credential of a configured connection.
CREDENTIAL = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{20,}|github_pat_\w{20,}|xox[abprs]-[A-Za-z0-9-]{10,}|AKIA[0-9A-Z]{16}"
                        r"|AIza[0-9A-Za-z_-]{30,})|\bBearer\s+\S{12,}")
MAX_DECLARATION_BYTES = 200_000
MAX_NAME = 200
MAX_DESCRIPTION = 2000
MAX_GUIDE = 4000
MAX_LABEL = 200
MAX_LABELS = 100
MAX_NOTE = 1000
MAX_NOTES = 20
MAX_ORIGIN = 200
MAX_VERSION = 1_000_000


def _invalid(message: str) -> JobError:
    return JobError(message, "invalid-template")


def text(value: Any, what: str, limit: int, *, required: bool = False) -> str:
    """A text field of a template: stripped, at most ``limit`` characters and, when ``required``, not empty."""
    if value is None and not required:
        return ""
    if not isinstance(value, str) or len(value) > limit or (required and not value.strip()):
        raise _invalid(f"'{what}' debe ser texto{' no vacío' if required else ''} de hasta {limit} caracteres.")
    return value.strip()


def guide(raw: Any) -> str:
    """``cases``: only its ``guide`` is written by a person; the fields come from the adapter and its evaluator."""
    if raw is None:
        return ""
    if not isinstance(raw, dict) or set(raw) - {"guide"}:
        raise _invalid("'cases' admite solo 'guide': el texto que explica cómo reunir casos de esta clase de tarea. "
                       "Sus campos ('fields') los fija el adaptador con su evaluador.")
    return text(raw.get("guide"), "cases.guide", MAX_GUIDE)


def presentation(raw: Any) -> dict[str, Any]:
    """The rules a review follows to present results: a name for the primary metric, a label per submetric and notes.

    They name and explain; they never change a score, a submetric or a recommendation. Without ``metricLabel``
    the review names the metric as its evaluator does.
    """
    raw = {} if raw is None else raw
    if not isinstance(raw, dict) or set(raw) - {"metricLabel", "submetrics", "notes"}:
        raise _invalid("'presentation' admite 'metricLabel' (cómo llamar a la métrica principal), 'submetrics' (una etiqueta por nombre de submétrica) "
                       "y 'notes' (notas para interpretar los resultados).")
    labels = raw.get("submetrics", {})
    if (not isinstance(labels, dict) or len(labels) > MAX_LABELS
            or not all(isinstance(name, str) and 0 < len(name) <= 100 and isinstance(label, str) and 0 < len(label.strip()) <= MAX_LABEL for name, label in labels.items())):
        raise _invalid(f"'presentation.submetrics' debe asociar hasta {MAX_LABELS} nombres de submétrica con una etiqueta de hasta {MAX_LABEL} caracteres.")
    notes = raw.get("notes", [])
    if not isinstance(notes, list) or len(notes) > MAX_NOTES or not all(isinstance(note, str) and 0 < len(note.strip()) <= MAX_NOTE for note in notes):
        raise _invalid(f"'presentation.notes' debe ser una lista de hasta {MAX_NOTES} notas de hasta {MAX_NOTE} caracteres.")
    label = text(raw.get("metricLabel"), "presentation.metricLabel", MAX_LABEL) or None
    return {"metricLabel": label, "submetrics": {name: value.strip() for name, value in sorted(labels.items())}, "notes": [note.strip() for note in notes]}


def _version(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not 1 <= value <= MAX_VERSION:
        raise _invalid("'version' debe ser un entero desde 1: el número con que las personas nombran esta versión de la plantilla.")
    return value


def _origin(raw: Any) -> dict[str, str] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict) or set(raw) - set(ORIGIN_KEYS) or not all(isinstance(value, str) and len(value) <= MAX_ORIGIN for value in raw.values()):
        raise _invalid(f"'origin' es solo información: {{{', '.join(repr(key) for key in ORIGIN_KEYS)}}} con textos de hasta {MAX_ORIGIN} caracteres.")
    return dict(raw)


def credential_field(value: Any, known: Sequence[str], path: str = "") -> str | None:
    """The first field of ``value`` whose text looks like a credential or holds a ``known`` one, or ``None``; keys are read as text too."""
    if isinstance(value, str):
        return (path or "solicitud") if CREDENTIAL.search(value) or any(secret in value for secret in known) else None
    if isinstance(value, dict):
        children = [(f"{path}.{key}" if path else str(key), child) for key, child in value.items()]
        if any(credential_field(str(key), known) for key in value):
            return path or "solicitud"
    elif isinstance(value, list):
        children = [(f"{path}[{index}]", child) for index, child in enumerate(value)]
    else:
        return None
    return next((found for where, child in children if (found := credential_field(child, known, where))), None)


def folder_name(name: str) -> str:
    """The folder a template saved without a path gets inside the templates folder: its name in lowercase ASCII words joined by hyphens."""
    plain = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "-", plain.lower()).strip("-")[:64].strip("-") or "plantilla"


def is_share_copy(folder: Path) -> bool:
    """A copy to share is recognised by its folder's suffix, whatever it holds: its holes are for a colleague to fill in a copy of their own."""
    return folder.name.endswith(SHARE_SUFFIX)


def share_copy_next(folder: Path) -> str:
    return (f"{folder.name} es una copia para compartir: sus huecos están por rellenar y nunca se aplica. Para usarla, cópiala a una carpeta con "
            f"otro nombre, sin {SHARE_SUFFIX}, rellena sus huecos y compruébala con: gepa template check <carpeta nueva>")


def examples() -> list[str]:
    """The names of GEPA's example templates."""
    return sorted(path.name for path in EXAMPLES.iterdir() if (path / DECLARATION).is_file()) if EXAMPLES.is_dir() else []


def example(name: str) -> Contents:
    """The folder of an example template, to copy as it is. Raises :class:`JobError` ``invalid-request`` for an unknown name."""
    if name not in examples():
        raise JobError(f"No hay una plantilla de ejemplo «{name}»; las disponibles son: {', '.join(examples()) or 'ninguna'}.", "invalid-request")
    return read(EXAMPLES / name)


def _quoted(path: Path | str) -> str:
    return f'"{path}"'


def _powershell(path: Path | str) -> str:
    """A literal argument of PowerShell: single quotes expand nothing, and a quote inside is doubled."""
    return "'" + str(path).replace("'", "''") + "'"


def trust_command(python: str, home: Path, folder: Path, sha256: str) -> dict[str, str]:
    """The command a person runs in their own terminal to trust the program of a template, ready to paste.

    It names this interpreter and this engine folder, so it runs as it is from any folder even without ``gepa`` on the PATH, and it pins the
    SHA-256 of the program the agent explained. ``command`` is for cmd, bash or zsh, with every path in double quotes; ``powershell`` calls the
    quoted interpreter with ``&``.
    """
    rest = f"template trust {{folder}} --sha256 {sha256}"
    return {"command": f"{_quoted(python)} -m gepa_engine --home {_quoted(home)} {rest.format(folder=_quoted(folder))}",
            "powershell": f"& {_powershell(python)} -m gepa_engine --home {_powershell(home)} {rest.format(folder=_powershell(folder))}"}


# The folder ------------------------------------------------------------------------------


def _problem(field: str, message: str, code: str = "invalid-template") -> dict[str, str]:
    return {"field": field, "code": code, "message": message}


@dataclass(frozen=True)
class Contents:
    """A template folder read at once, so its check, its identity and its frozen copy see the same bytes.

    ``problems`` are the ones found while reading: an entry that does not belong, or an adapter folder that cannot be read.
    """

    declaration: bytes
    adapter: Folder | None = None
    problems: tuple[dict[str, str], ...] = ()

    @property
    def folder(self) -> Folder:
        """The whole folder, as its identity and its frozen copy take it: the declaration and the adapter's files."""
        adapter = self.adapter.files if self.adapter else ()
        return Folder(files=tuple(sorted([(DECLARATION, self.declaration), *((f"{ADAPTER_DIR}/{path}", data) for path, data in adapter)])))

    @property
    def sha256(self) -> str | None:
        """The template's identity, when every file could be read: two contents never share it, whatever version they declare."""
        return None if any(problem["field"] == ADAPTER_DIR for problem in self.problems) else self.folder.sha256


def contents(declared: Mapping[str, Any], adapter: Folder | None) -> Contents:
    """The folder a template saved now would have: ``declared`` as ``template.json`` and, with an agent's adapter, its files."""
    return Contents(declaration=(json.dumps(declared, ensure_ascii=False, indent=2) + "\n").encode("utf-8"), adapter=adapter)


def read(folder: Path) -> Contents:
    """The files of a template folder; hidden entries are not part of it. Raises :class:`JobError` ``template-not-found``."""
    try:
        with os.scandir(folder) as found:
            entries = sorted(found, key=lambda entry: entry.name)
    except OSError:
        entries = []
    if not any(entry.name == DECLARATION for entry in entries):
        raise JobError(f"{folder} no es una plantilla: debe ser una carpeta con {DECLARATION}. Busca las disponibles con 'gepa template list'.", "template-not-found")
    problems = []
    for entry in entries:
        path = Path(entry.path)
        if entry.name.startswith("."):
            continue
        if entry.name == DECLARATION and (path.is_symlink() or not path.is_file()):
            problems.append(_problem(DECLARATION, f"{DECLARATION} debe ser un archivo normal, no un enlace; copia el archivo real."))
        elif entry.name == ADAPTER_DIR and (path.is_symlink() or path.is_junction() or not path.is_dir()):
            problems.append(_problem(ADAPTER_DIR, f"{ADAPTER_DIR}/ debe ser una carpeta normal, no un enlace; copia su contenido real."))
        elif entry.name not in (DECLARATION, ADAPTER_DIR):
            problems.append(_problem(entry.name, f"'{entry.name}' sobra: la carpeta de una plantilla solo lleva {DECLARATION} y, con un adaptador propio, "
                                                 f"la carpeta {ADAPTER_DIR}/ con sus archivos."))
    try:
        with open(folder / DECLARATION, "rb") as handle:
            data = handle.read(MAX_DECLARATION_BYTES + 1)
    except OSError:
        raise JobError(f"No se pudo leer {folder / DECLARATION}.", "template-not-found") from None
    if len(data) > MAX_DECLARATION_BYTES:
        problems.append(_problem(DECLARATION, f"{DECLARATION} supera {MAX_DECLARATION_BYTES // 1000} KB."))
    adapter = None
    if (folder / ADAPTER_DIR).is_dir() and not any(problem["field"] == ADAPTER_DIR for problem in problems):
        try:
            adapter = read_folder(folder / ADAPTER_DIR)
        except AdapterError as error:
            problems.append(_problem(ADAPTER_DIR, str(error), error.code))
    return Contents(declaration=data, adapter=adapter, problems=tuple(problems))


def write(target: Path, template: Contents) -> None:
    """Write a new template folder. A folder that exists, even an empty one, is never overwritten."""
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.mkdir()
    except FileExistsError:
        raise JobError(f"Ya existe {target}: guardar una plantilla nunca sobrescribe una carpeta. Elige otro nombre, o indica otra carpeta en 'path'.",
                       "template-exists") from None
    except OSError:
        raise JobError(f"No se pudo crear la carpeta {target}; comprueba permisos y espacio libre.", "template-write-failed") from None
    try:
        write_folder(target, template.folder)
    except OSError:
        shutil.rmtree(target, ignore_errors=True)
        raise JobError(f"No se pudo escribir la plantilla en {target}; comprueba permisos y espacio libre.", "template-write-failed") from None


class Store:
    """Frozen copies of the templates drafts were prepared with, in the data folder, each in a folder named after its SHA-256 like an adapter's."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, sha256: str) -> Path:
        return self.root / sha256[:16]

    def freeze(self, template: Contents) -> Path:
        """The frozen copy of ``template``, written now unless an intact one already exists."""
        sha256 = template.folder.sha256
        target = self.path(sha256)
        if self._intact(target, sha256):
            return target
        shutil.rmtree(target, ignore_errors=True)
        temporary = self.root / f".tmp-{uuid4().hex[:12]}"
        try:
            write_folder(temporary, template.folder)
            os.replace(temporary, target)
        except OSError:
            shutil.rmtree(temporary, ignore_errors=True)
            if self._intact(target, sha256):  # another process froze the same template meanwhile
                return target
            raise JobError(f"No se pudo guardar la copia congelada de la plantilla en {self.root}; comprueba permisos y espacio libre.",
                           "workspace-unavailable") from None
        return target

    @staticmethod
    def _intact(target: Path, sha256: str) -> bool:
        try:
            return target.is_dir() and read(target).sha256 == sha256
        except JobError:
            return False


# The check -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Checked:
    """A template folder checked without the original: its declaration and what the engine deduces of its adapter, or its problems field by field."""

    contents: Contents
    declaration: dict[str, Any] | None
    deduced: dict[str, Any] | None  # the adapter's identity, its evaluator with its rule, and the case fields
    problems: tuple[dict[str, str], ...]

    @property
    def valid(self) -> bool:
        return not self.problems


def _agent(adapter: Folder, declared: Mapping[str, Any]) -> dict[str, Any]:
    """What an approval seals of an agent's adapter (as ``CustomAdapter.describe`` gives it), read from its declaration without loading its program."""
    evaluation = declared["evaluation"]
    return {"adapter": {"name": declared["name"], "version": declared["version"], "sha256": adapter.sha256, "files": adapter.digests(),
                        "description": declared["description"], "requirements": declared["requirements"]},
            "evaluator": {"name": evaluation["name"], "version": evaluation["version"], "primaryMetric": evaluation["primaryMetric"], "higherIsBetter": True,
                          "description": evaluation["description"], "aggregation": {"rule": evaluation["rule"]}},
            "fields": sorted(["input", *declared["input"]["evaluatorFields"]])}


def check(template: Contents, credentials: Sequence[str]) -> Checked:
    """Validate a template read from its folder, field by field, without the original and without loading its adapter's program.

    ``credentials`` are the values of the configured connections' credentials: a problem names the field that holds one, never the value.
    """
    problems = list(template.problems)
    try:
        raw = json.loads(template.declaration.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raw = None
    if not isinstance(raw, dict):
        return Checked(template, None, None, (*problems, _problem(DECLARATION, f"{DECLARATION} debe contener un objeto JSON válido en UTF-8.")))
    for secret in dict.fromkeys(field for field in (find_secret_key(raw), credential_field(raw, credentials)) if field):
        problems.append(_problem(secret, f"'{secret}' parece contener una credencial: una plantilla nunca guarda credenciales; quítala y configúrala en las conexiones.",
                                 "secret-in-template"))
    for key in sorted(set(raw) - set(REQUIRED) - set(OPTIONAL)):
        problems.append(_problem(key, f"'{key}' no es un campo de {DECLARATION}; admite {', '.join(REQUIRED + OPTIONAL)}."))
    for key in ("format", "artifactType", "adapter"):  # a missing name or version is its validator's problem
        if key not in raw:
            problems.append(_problem(key, f"Falta '{key}' en {DECLARATION}."))

    def field(name: str, validate: Any) -> Any:
        """The value ``validate`` returns, or ``None`` with its error as the problem of ``name``."""
        try:
            return validate()
        except JobError as error:
            problems.append(_problem(name, str(error), error.code))
            return None

    if "format" in raw and raw["format"] != FORMAT:
        problems.append(_problem("format", f"'format' debe ser \"{FORMAT}\"."))
    declared = {"format": FORMAT, "name": field("name", lambda: text(raw.get("name"), "name", MAX_NAME, required=True)),
                "description": field("description", lambda: text(raw.get("description"), "description", MAX_DESCRIPTION)),
                "version": field("version", lambda: _version(raw.get("version"))), "artifactType": raw.get("artifactType"), "adapter": raw.get("adapter")}
    kind = KINDS.get(raw["artifactType"]) if isinstance(raw.get("artifactType"), str) else None
    if "artifactType" in raw and kind is None:
        problems.append(_problem("artifactType", f"'artifactType' debe ser uno de: {', '.join(KINDS)}."))
    deduced = None
    adapter, files = raw.get("adapter"), template.adapter
    if adapter == {"folder": ADAPTER_DIR}:
        for name in ("executor", "evaluator"):
            if raw.get(name) is not None:
                problems.append(_problem(name, f"'{name}' no se indica con un adaptador propio: su adapter.json declara el ejecutor y el evaluador.", f"invalid-{name}"))
        if files is None:
            if not any(problem["field"] == ADAPTER_DIR for problem in problems):
                problems.append(_problem("adapter", f"'adapter' nombra la carpeta {ADAPTER_DIR}/, que no está en la plantilla."))
        else:
            agent = field("adapter", lambda: parse_declaration(files))  # its adapter.json, without importing adapter.py
            if agent is not None and kind is not None and agent["artifact"] != kind.type:
                problems.append(_problem("adapter", f"El adaptador {agent['name']} es para artefactos de tipo {agent['artifact']!r}, no {kind.type!r}."))
            deduced = None if agent is None else _agent(files, agent)
    elif isinstance(adapter, dict) and set(adapter) == {"name", "version"}:
        if files is not None:
            problems.append(_problem(ADAPTER_DIR, f"La carpeta {ADAPTER_DIR}/ solo va con un adaptador propio: \"adapter\": {{\"folder\": \"{ADAPTER_DIR}\"}}."))
        if kind is not None:
            builtin = kind.deduced()["adapter"]
            if adapter != builtin:
                problems.append(_problem("adapter", f"El adaptador incluido para {kind.type} es {builtin['name']} v{builtin['version']}: "
                                                    f"\"adapter\": {json.dumps(builtin, ensure_ascii=False)}."))
            for name in ("executor", "evaluator"):  # each setting on its own, so a problem in both shows both
                field(name, lambda name=name: kind.deduced(**{name: raw.get(name)}))
            if not any(problem["field"] in ("executor", "evaluator") for problem in problems):
                settings = {name: raw.get(name) for name in ("executor", "evaluator")}
                deduced = kind.deduced(**settings)
                declared.update(settings)
    elif "adapter" in raw:
        problems.append(_problem("adapter", "'adapter' debe ser el adaptador incluido, {\"name\": \"<nombre>\", \"version\": \"<versión>\"}, "
                                            f"o el propio de la plantilla, {{\"folder\": \"{ADAPTER_DIR}\"}}, con sus archivos en {ADAPTER_DIR}/."))
    declared.update(cases={"guide": field("cases", lambda: guide(raw.get("cases")))}, presentation=field("presentation", lambda: presentation(raw.get("presentation"))),
                    origin=field("origin", lambda: _origin(raw.get("origin"))))
    if problems:
        return Checked(template, None, None, tuple(problems))
    return Checked(template, declared, deduced, ())


def require_valid(checked: Checked, what: str, retry: str) -> None:
    """Refuse a template with problems, with the code of the first one (a credential first) and a message that never shows a value."""
    if checked.valid:
        return
    first = next((problem for problem in checked.problems if problem["code"] == "secret-in-template"), checked.problems[0])
    more = f" (y {len(checked.problems) - 1} problemas más)" if len(checked.problems) > 1 else ""
    raise JobError(f"{what} no es válida en '{first['field']}': {first['message']}{more} {retry}", first["code"])


# Views -----------------------------------------------------------------------------------


def view(folder: Path, checked: Checked) -> dict[str, Any]:
    """The template a person inspects before applying it, with what the engine deduces, its hash and how to apply it; or its problems, field by field.

    A copy to share is shown as any other, but without how to apply it: it never is.
    """
    shared = is_share_copy(folder)
    base = {"path": str(folder), "sha256": checked.contents.sha256, "valid": checked.valid, "shareCopy": shared, "problems": list(checked.problems)}
    where = folder.as_posix()
    if checked.declaration is None or checked.deduced is None:
        return {**base, "next": f"Corrige cada problema en los archivos de la plantilla y vuelve a comprobarla con: gepa template check {where}"}
    declared, deduced = checked.declaration, checked.deduced
    shown = {**base, "name": declared["name"], "description": declared["description"], "version": declared["version"], "artifactType": declared["artifactType"],
             "adapter": deduced["adapter"], "executor": declared.get("executor"), "evaluator": declared.get("evaluator"), "evaluation": deduced["evaluator"],
             "cases": {"fields": deduced["fields"], "guide": declared["cases"]["guide"]}, "presentation": declared["presentation"], "origin": declared["origin"]}
    if shared:
        return {**shown, "next": share_copy_next(folder)}
    request = {"template": {"path": str(folder)}, "artifact": {"type": declared["artifactType"], "path": "<original>"},
               "objective": "<qué debe mejorar y qué proteger>", "inputs": [{"path": "<casos nuevos>"}]}
    return {**shown, "apply": {"request": request, "cli": "gepa dataset prepare <prepare.json>", "tool": {"name": "gepa_dataset_prepare", "arguments": {"request": request}}},
            "next": (f"Aplica la plantilla con \"template\": {{\"path\": \"{where}\"}} en la solicitud de preparación, con el original, el objetivo y los casos "
                     f"nuevos. Para cambiarla, edita sus archivos y vuelve a comprobarla con: gepa template check {where}")}


def summary(folder: Path, checked: Checked) -> dict[str, Any]:
    """One line of the template list: a valid template by name, description, version, artifact type and adapter; one with problems, by its problems."""
    if checked.declaration is None or checked.deduced is None:
        return {"path": str(folder), "valid": False, "problems": list(checked.problems)}
    declared, adapter = checked.declaration, checked.deduced["adapter"]
    return {"path": str(folder), "valid": True, "name": declared["name"], "description": declared["description"], "version": declared["version"],
            "artifactType": declared["artifactType"],
            "adapter": {name: adapter[name] for name in ("name", "version", "sha256") if name in adapter}, "sha256": checked.contents.sha256}


def application(folder: Path, checked: Checked) -> dict[str, Any]:
    """What a draft prepared with the template records of it, with an application id of its own: each application is a new draft, even with the same cases."""
    assert checked.declaration is not None
    declared = checked.declaration
    return {"path": str(folder), "sha256": checked.contents.sha256, "name": declared["name"], "version": declared["version"],
            "presentation": declared["presentation"], "applicationId": "apl-" + uuid4().hex[:12]}
