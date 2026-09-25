"""The folder and the declaration of an adapter an agent prepares: its identity, its frozen copies and what ``adapter.json`` declares.

An adapter is a folder with ``adapter.json``, ``adapter.py`` and resources of its own. Its identity is the
SHA-256 of its files; hidden entries and ``__pycache__`` are not part of it. Preparing a dataset freezes a
copy of the folder, named after that hash, in the data folder; the draft, the approval and every job
record the hash, and a change of the source folder afterwards is refused until the dataset is prepared
and approved again.

``adapter.json`` declares the task. The executor receives only the case's ``input``; the other case
fields are the evaluator's, like ``expected``. It also declares the resources, tools and environment of a
case, its observable output, the evaluation and its rule, the model roles with their output limits, and
the requirements to check before any model call.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

from .artifacts import KINDS, request_path
from .datasets import GENERIC_FIELDS
from .encoding import is_utf8
from .errors import JobError
from .evaluation import JUDGE_ROLE, RUBRIC_JUDGE
from .jev import ADAPTER as JEV_ADAPTER, EVALUATOR as JEV_EVALUATOR
from .skill import ADAPTER as SKILL_ADAPTER, EVALUATOR as SKILL_EVALUATOR

FORMAT = "gepa-adapter-v1"
DECLARATION = "adapter.json"
CODE = "adapter.py"
EXAMPLES = Path(__file__).resolve().parent / "adapter_examples"  # adapters `gepa adapter init` copies
DEFAULT_EXAMPLE = "python-tests"
DECLARATION_KEYS = ("format", "name", "version", "artifact", "description", "input", "resources", "tools", "environment", "output", "evaluation", "roles")
BUILTIN_ADAPTERS = frozenset(str(item["name"]) for item in (JEV_ADAPTER, SKILL_ADAPTER))
BUILTIN_EVALUATORS = frozenset(str(item["name"]) for item in (JEV_EVALUATOR, SKILL_EVALUATOR, RUBRIC_JUDGE))
ROLES = ("executor", JUDGE_ROLE)  # model roles an adapter may use: the executor in execute, the judge in evaluate; the reflection is the engine's
REQUIREMENT_KINDS = ("python", "command", "credential")
# What a case record holds besides the case's own fields (this adapter's and the built-in ones'): a declared field may not take one of these names.
RECORD_KEYS = frozenset({"caseId", "phase", "output", "score", "feedback", "error", "submetrics", "requirementsMet", "taskSuccess", "observation", "effects",
                         "steps", "trace", "latencyMs", "usage", "costUsd", "decision", "checks", "session", "judge", "rubric"})
RESERVED_FIELDS = GENERIC_FIELDS | RECORD_KEYS
NAME = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")
FIELD = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*")
VARIABLE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}")
PATH = re.compile(r"[^\x00-\x1f]{1,150}")
MAX_TEXT = 2000
MAX_FIELDS = 20
MAX_TOOLS = 20
MAX_REQUIREMENTS = 30
MAX_FILES = 50
MAX_BYTES = 2_000_000
MAX_OUTPUT_TOKENS = 32768  # the output limit of any model call of a job


class AdapterError(JobError):
    """The adapter cannot be used or broke its contract; during an evaluation it stops it without scoring the candidate."""


# The folder ------------------------------------------------------------------------------


@dataclass(frozen=True)
class Folder:
    """An adapter folder's files by posix path, as bytes, sorted by path; hidden entries and ``__pycache__`` are not part of it."""

    files: tuple[tuple[str, bytes], ...]

    @property
    def sha256(self) -> str:
        """The folder's identity: every path and its bytes."""
        digest = hashlib.sha256()
        for path, data in self.files:
            digest.update(path.encode("utf-8") + b"\0" + str(len(data)).encode() + b"\0" + data)
        return digest.hexdigest()

    def digests(self) -> dict[str, str]:
        return {path: hashlib.sha256(data).hexdigest() for path, data in self.files}

    def data(self, path: str) -> bytes:
        return dict(self.files)[path]


def read_folder(folder: Path) -> Folder:
    """The files of an adapter folder. Raises :class:`AdapterError`: ``adapter-unreadable`` or ``invalid-adapter``."""
    try:
        root = folder.resolve(strict=True)
        if not root.is_dir():
            raise NotADirectoryError(str(root))
    except OSError:
        raise AdapterError(f"No se pudo leer la carpeta del adaptador {folder}: debe existir y contener {DECLARATION} y {CODE}.", "adapter-unreadable") from None

    def unreadable(error: OSError) -> None:
        raise error  # os.walk would otherwise skip a folder it cannot read, leaving the adapter incomplete

    files: list[tuple[str, bytes]] = []
    total = 0
    try:
        for current, directories, names in os.walk(root, onerror=unreadable):
            here = Path(current)
            for name in list(directories):
                if (here / name).is_symlink() or (here / name).is_junction():
                    raise AdapterError(f"El adaptador contiene un enlace a otra carpeta ({(here / name).relative_to(root).as_posix()}); copia su contenido real.",
                                       "invalid-adapter")
            directories[:] = sorted(name for name in directories if not name.startswith(".") and name != "__pycache__")
            for name in sorted(names):
                if name.startswith("."):
                    continue
                entry = here / name
                relative = entry.relative_to(root).as_posix()
                if entry.is_symlink() or not entry.is_file():
                    raise AdapterError(f"'{relative}' no es un archivo normal (es un enlace u otro tipo); copia el archivo real.", "invalid-adapter")
                data = entry.read_bytes()
                total += len(data)
                if len(files) >= MAX_FILES or total > MAX_BYTES:
                    raise AdapterError(f"El adaptador supera {MAX_FILES} archivos o {MAX_BYTES // 1_000_000} MB.", "invalid-adapter")
                files.append((relative, data))
    except OSError:
        raise AdapterError(f"No se pudo leer la carpeta del adaptador {folder}.", "adapter-unreadable") from None
    present = {path for path, _ in files}
    missing = [name for name in (DECLARATION, CODE) if name not in present]
    if missing:
        raise AdapterError(f"La carpeta del adaptador {folder} debe tener {DECLARATION} y {CODE} en su raíz; falta {', '.join(missing)}.", "invalid-adapter")
    return Folder(files=tuple(sorted(files)))


def write_folder(target: Path, contents: Folder) -> None:
    """Write every file of ``contents`` under ``target``, byte for byte. Raises :class:`OSError`."""
    for path, data in contents.files:
        file = target.joinpath(*path.split("/"))
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_bytes(data)


def request_folder(raw: Any, base_dir: Path | str | None) -> Path:
    """The folder of ``{"path": "<carpeta>"}`` in a prepare request, relative to the request's folder."""
    if not isinstance(raw, dict) or set(raw) != {"path"}:
        raise AdapterError("'adapter' debe ser {\"path\": \"<carpeta con adapter.json y adapter.py>\"}.", "invalid-adapter")
    return request_path(raw["path"], base_dir, "adapter.path", "invalid-adapter").resolve()


def require_unchanged(source: str | None, sha256: str) -> None:
    """Refuse a prepared adapter whose source folder changed: the change would otherwise pass silently.

    A source folder that is gone is not a change: the frozen copy is the prepared version.
    """
    if source is None or not Path(source).is_dir():
        return
    try:
        current: str | None = read_folder(Path(source)).sha256
    except AdapterError:
        current = None
    if current != sha256:
        raise AdapterError(f"El adaptador de {source} cambió desde que se preparó este dataset (se preparó {sha256[:12]}): prepáralo, previsualízalo y apruébalo "
                           "de nuevo para usar los cambios, o restaura la versión preparada.", "adapter-changed")


class Store:
    """Frozen copies of prepared adapters in the data folder, each in a folder named after its SHA-256."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def path(self, sha256: str) -> Path:
        return self.root / sha256[:16]

    def freeze(self, contents: Folder) -> Path:
        """The frozen copy of ``contents``, written now unless an intact one already exists."""
        target = self.path(contents.sha256)
        if self._intact(target, contents.sha256):
            return target
        shutil.rmtree(target, ignore_errors=True)
        temporary = self.root / f".tmp-{uuid4().hex[:12]}"
        try:
            write_folder(temporary, contents)
            os.replace(temporary, target)
        except OSError:
            shutil.rmtree(temporary, ignore_errors=True)
            if self._intact(target, contents.sha256):  # another process froze the same adapter meanwhile
                return target
            raise AdapterError(f"No se pudo guardar la copia congelada del adaptador en {self.root}; comprueba permisos y espacio libre.", "workspace-unavailable") from None
        return target

    def existing(self, sha256: str) -> Path:
        """The frozen copy an approval or a manifest recorded. Loading it reads its content again, so a changed copy has another hash."""
        target = self.path(sha256)
        if not (target / DECLARATION).is_file():
            raise AdapterError(f"La copia congelada del adaptador {sha256[:12]} ya no está en la carpeta de datos ({target}); prepara y aprueba el dataset de nuevo.",
                               "adapter-missing")
        return target

    @staticmethod
    def _intact(target: Path, sha256: str) -> bool:
        try:
            return target.is_dir() and read_folder(target).sha256 == sha256
        except AdapterError:
            return False


def examples() -> list[str]:
    return sorted(path.name for path in EXAMPLES.iterdir() if (path / DECLARATION).is_file()) if EXAMPLES.is_dir() else []


def init(target: Path, example: str = DEFAULT_EXAMPLE) -> dict[str, Any]:
    """``gepa adapter init``: copy an example adapter into a new or empty folder, for the agent to adapt to its task."""
    if example not in examples():
        raise AdapterError(f"No hay un ejemplo de adaptador «{example}»; los disponibles son: {', '.join(examples()) or 'ninguno'}.", "invalid-request")
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise AdapterError(f"{target} ya existe y no está vacía; elige una carpeta nueva.", "adapter-target-not-empty")
    contents = read_folder(EXAMPLES / example)
    try:
        write_folder(target, contents)
    except OSError:
        raise AdapterError(f"No se pudo escribir en {target}.", "adapter-init-failed") from None
    declaration = parse_declaration(contents)
    return {"path": str(target), "example": example, "files": [path for path, _ in contents.files],
            "adapter": {"name": declaration["name"], "version": declaration["version"], "artifact": declaration["artifact"]},
            "next": (f"Adapta {DECLARATION} (entrada, recursos, herramientas, entorno, salida, evaluación, roles y requisitos) y {CODE} (execute, evaluate, "
                     f"check_case y check) a la tarea; cambia 'name'. Luego comprueba con: gepa adapter check {target.as_posix()}")}


# The declaration -------------------------------------------------------------------------


def _problem(message: str) -> AdapterError:
    return AdapterError(f"{DECLARATION}: {message}", "invalid-adapter")


def _object(value: Any, what: str, required: Sequence[str], optional: Sequence[str] = ()) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _problem(f"{what} debe ser un objeto JSON.")
    missing = [key for key in required if key not in value]
    unknown = sorted(set(value) - set(required) - set(optional))
    if missing or unknown:
        fields = ", ".join(required) + (f" y, opcionalmente, {', '.join(optional)}" if optional else "")
        found = f"falta {', '.join(missing)}" if missing else f"sobra {', '.join(unknown)}"
        raise _problem(f"{what} lleva {fields}; {found}.")
    return value


def _text(value: Any, what: str, limit: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit or not is_utf8(value):
        raise _problem(f"{what} debe ser texto de 1 a {limit} caracteres.")
    return value.strip()


def _flag(value: Any, what: str) -> bool:
    if not isinstance(value, bool):
        raise _problem(f"{what} debe ser true o false.")
    return value


def _names(value: Any, what: str, pattern: re.Pattern[str], *, minimum: int = 0, maximum: int = MAX_FIELDS) -> list[str]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum or not all(isinstance(item, str) and pattern.fullmatch(item) for item in value):
        raise _problem(f"{what} debe ser una lista de {minimum} a {maximum} nombres (letras, cifras y '_', empezando por una letra).")
    if len(set(value)) != len(value):
        raise _problem(f"{what} repite un nombre.")
    return list(value)


def _input(raw: Any) -> dict[str, Any]:
    """The case's entry, which the executor receives, and the fields only the evaluator reads."""
    value = _object(raw, "'input'", ("description", "evaluatorFields"), ("optional",))
    evaluator = _names(value["evaluatorFields"], "'input.evaluatorFields'", FIELD)
    optional = _names(value.get("optional", []), "'input.optional'", FIELD)
    if "input" in evaluator:
        raise _problem("'input' es la entrada del caso y la recibe el ejecutor: todo lo que ve va dentro de 'input' (texto u objeto JSON), y "
                       "'input.evaluatorFields' nombra lo que solo lee el evaluador, como 'expected'.")
    reserved = [field for field in evaluator if field in RESERVED_FIELDS]
    if reserved:
        raise _problem(f"'{reserved[0]}' es un nombre del motor (partición, procedencia o evidencia del caso): usa otro nombre de campo.")
    if any(field not in evaluator for field in optional):
        raise _problem("'input.optional' solo nombra campos de 'input.evaluatorFields'.")
    return {"description": _text(value["description"], "'input.description'"), "evaluatorFields": evaluator, "optional": optional}


def _tools(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list) or len(raw) > MAX_TOOLS:
        raise _problem(f"'tools' debe ser una lista de hasta {MAX_TOOLS} herramientas {{\"name\", \"description\"}} (vacía si el ejecutor no usa ninguna).")
    tools = []
    for number, item in enumerate(raw, 1):
        tool = _object(item, f"la herramienta {number}", ("name", "description"))
        tools.append({"name": _text(tool["name"], f"'name' de la herramienta {number}", 100), "description": _text(tool["description"], f"'description' de la herramienta {number}")})
    if len({tool["name"] for tool in tools}) != len(tools):
        raise _problem("dos herramientas tienen el mismo 'name'.")
    return tools


def _roles(raw: Any) -> dict[str, dict[str, Any]]:
    if not isinstance(raw, dict) or "executor" not in raw or set(raw) - set(ROLES):
        raise _problem("'roles' declara 'executor' y, si el evaluador consulta un juez, 'judge'; cada uno con 'maxTokens' (tokens de salida por llamada) y, opcionalmente, 'purpose'.")
    roles = {}
    for role in ROLES:
        if role not in raw:
            continue
        value = _object(raw[role], f"'roles.{role}'", ("maxTokens",), ("purpose",))
        tokens = value["maxTokens"]
        if not isinstance(tokens, int) or isinstance(tokens, bool) or not 1 <= tokens <= MAX_OUTPUT_TOKENS:
            raise _problem(f"'roles.{role}.maxTokens' debe ser un entero entre 1 y {MAX_OUTPUT_TOKENS}.")
        roles[role] = {"maxTokens": tokens, **({"purpose": _text(value["purpose"], f"'roles.{role}.purpose'")} if "purpose" in value else {})}
    return roles


def _requirements(raw: Any) -> list[dict[str, str]]:
    if not isinstance(raw, list) or len(raw) > MAX_REQUIREMENTS:
        raise _problem(f"'requirements' debe ser una lista de hasta {MAX_REQUIREMENTS} requisitos {{\"kind\", \"name\", \"purpose\"}}.")
    requirements, seen = [], set()
    patterns = {"python": MODULE, "command": re.compile(r"[^\x00-\x1f]{1,200}"), "credential": VARIABLE}
    for number, item in enumerate(raw, 1):
        value = _object(item, f"el requisito {number}", ("kind", "name", "purpose"), ("install",))
        kind, name = value["kind"], value["name"]
        if kind not in REQUIREMENT_KINDS:
            raise _problem(f"'kind' del requisito {number} debe ser uno de: {', '.join(REQUIREMENT_KINDS)} (un navegador, una API o un sandbox se comprueban en check()).")
        if not isinstance(name, str) or not patterns[kind].fullmatch(name):
            what = {"python": "el nombre con que se importa el módulo", "command": "el nombre del comando", "credential": "el nombre de la variable de entorno"}[kind]
            raise _problem(f"'name' del requisito {number} debe ser {what}.")
        if (kind, name) in seen:
            raise _problem(f"el requisito {kind}:{name} se repite.")
        seen.add((kind, name))
        requirements.append({"kind": kind, "name": name, "purpose": _text(value["purpose"], f"'purpose' del requisito {number}"),
                             **({"install": _text(value["install"], f"'install' del requisito {number}", 500)} if "install" in value else {})})
    return requirements


def parse_declaration(contents: Folder) -> dict[str, Any]:
    """The validated ``adapter.json`` of an adapter folder: every key of the contract, texts trimmed. Raises :class:`AdapterError`."""
    try:
        raw = json.loads(contents.data(DECLARATION).decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError):
        raise _problem("no contiene JSON válido en UTF-8.") from None
    value = _object(raw, DECLARATION, DECLARATION_KEYS, ("requirements",))
    if value["format"] != FORMAT:
        raise _problem(f"'format' debe ser \"{FORMAT}\".")
    name = value["name"]
    if not isinstance(name, str) or not NAME.fullmatch(name):
        raise _problem("'name' debe ser un identificador de minúsculas, cifras y guiones (hasta 64 caracteres), como \"python-tests\".")
    if name in BUILTIN_ADAPTERS:
        raise _problem(f"'name' no puede ser «{name}»: es un adaptador incluido en el motor.")
    if value["artifact"] not in KINDS:
        raise _problem(f"'artifact' debe ser uno de: {', '.join(KINDS)}; es el tipo de artefacto que GEPA optimiza con este adaptador.")
    resources = _object(value["resources"], "'resources'", ("description",), ("files",))
    files = _names(resources.get("files", []), "'resources.files'", PATH, maximum=MAX_FILES)
    present = {path for path, _ in contents.files}
    absent = [path for path in files if path not in present]
    if absent:
        raise _problem(f"'resources.files' nombra archivos que no están en la carpeta del adaptador: {', '.join(absent)}.")
    environment = _object(value["environment"], "'environment'", ("description", "network", "codeExecution", "isolation"))
    output = _object(value["output"], "'output'", ("description",))
    evaluation = _object(value["evaluation"], "'evaluation'", ("name", "version", "primaryMetric", "description", "rule"))
    if not isinstance(evaluation["name"], str) or not NAME.fullmatch(evaluation["name"]) or evaluation["name"] in BUILTIN_EVALUATORS:
        raise _problem("'evaluation.name' debe ser un identificador de minúsculas, cifras y guiones, distinto de los evaluadores del motor "
                       f"({', '.join(sorted(BUILTIN_EVALUATORS))}).")
    if not isinstance(evaluation["primaryMetric"], str) or not FIELD.fullmatch(evaluation["primaryMetric"]):
        raise _problem("'evaluation.primaryMetric' debe ser un nombre como \"testPassRate\": el score por caso, de 0 a 1, donde más alto es mejor.")
    return {
        "format": FORMAT, "name": name, "version": _text(value["version"], "'version'", 40), "artifact": value["artifact"],
        "description": _text(value["description"], "'description'"), "input": _input(value["input"]),
        "resources": {"description": _text(resources["description"], "'resources.description'"), "files": files},
        "tools": _tools(value["tools"]),
        "environment": {"description": _text(environment["description"], "'environment.description'"), "network": _flag(environment["network"], "'environment.network'"),
                        "codeExecution": _flag(environment["codeExecution"], "'environment.codeExecution'"),
                        "isolation": _text(environment["isolation"], "'environment.isolation'")},
        "output": {"description": _text(output["description"], "'output.description'")},
        "evaluation": {"name": evaluation["name"], "version": _text(evaluation["version"], "'evaluation.version'", 40), "primaryMetric": evaluation["primaryMetric"],
                       "description": _text(evaluation["description"], "'evaluation.description'"), "rule": _text(evaluation["rule"], "'evaluation.rule'")},
        "roles": _roles(value["roles"]), "requirements": _requirements(value.get("requirements", [])),
    }
