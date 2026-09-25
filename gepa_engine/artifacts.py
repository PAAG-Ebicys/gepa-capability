"""Artifact kinds: how the engine reads, names, shows and exports each type of artifact it optimizes.

``jobs.py`` looks a kind up by the artifact's ``type`` and never branches on one. A kind reads the
original from a request (a policy file or inline JSON, a Skill folder), builds the adapter that runs
and scores its cases from the executor and evaluator settings a draft seals, shows the artifact in a
dataset view and lays out an exported candidate. How one case runs and scores is the adapter's.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any, ClassVar, Protocol, cast

from .errors import JobError
from .evaluation import EvaluatorError, JudgeSettings
from .jev import ADAPTER as JEV_ADAPTER, EVALUATOR as JEV_EVALUATOR, JevChoiceAdapter, PolicyError, PolicySurface, parse_policy
from .skill import (ADAPTER as SKILL_ADAPTER, SKILL_DIR, Skill, SkillError, SkillSessionAdapter, SkillSurface, load_folder, parse_evaluator, parse_session, parse_skill,
                    sealed_evaluator, sealed_session)


class Adapter(Protocol):
    """What the engine needs from an experiment adapter: how one case runs and scores, and the only surface GEPA may change."""

    @property
    def artifact_type(self) -> str: ...
    @property
    def roles(self) -> Mapping[str, str]: ...  # job role -> role configured by setup-gepa
    @property
    def executor_role(self) -> str: ...
    @property
    def component(self) -> str: ...
    @property
    def modules(self) -> tuple[str, ...]: ...
    @property
    def limit_defaults(self) -> Mapping[str, int]: ...
    @property
    def metric_label(self) -> str: ...
    @property
    def preview_max_tokens(self) -> int: ...
    @property
    def case_fields(self) -> frozenset[str]: ...
    @property
    def execution(self) -> Mapping[str, Any]: ...
    @property
    def labels(self) -> tuple[str, ...]: ...

    def describe(self) -> dict[str, Any]: ...
    def original(self) -> Any: ...
    def verify_requirements(self) -> None: ...
    def check_case(self, case: Mapping[str, Any]) -> str | None: ...
    def validate_case(self, case: Mapping[str, Any]) -> None: ...
    def seed(self) -> dict[str, str]: ...
    def artifact(self, candidate: Mapping[str, str]) -> Any: ...
    def run_case(self, candidate: Mapping[str, str], case: Mapping[str, Any], request: Any) -> dict[str, Any]: ...
    def reflective_record(self, result: Mapping[str, Any]) -> dict[str, Any]: ...
    def reflection_messages(self, candidate: Mapping[str, str], records: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]: ...
    def parse_proposal(self, text: Any) -> dict[str, str]: ...


class Surface(Protocol):
    """What GEPA may change in an artifact, and how a candidate of it is stored, handed to a case and proposed again.

    Every adapter of an artifact type shares its surface: the built-in one and the adapters an agent prepares.
    """

    @property
    def component(self) -> str: ...

    def seed(self) -> dict[str, str]: ...
    def artifact(self, candidate: Mapping[str, str]) -> Any: ...  # what a job stores of a candidate
    def document(self, candidate: Mapping[str, str]) -> Any: ...  # the whole candidate artifact, as a case receives it
    def materialize(self, candidate: Mapping[str, str], root: Path) -> Path: ...  # the candidate written into a workspace
    def describe(self) -> dict[str, Any]: ...  # the mutable surface an approval seals
    def fixed(self) -> dict[str, Any]: ...  # the artifact's part of the fixed contract
    def reflection_messages(self, candidate: Mapping[str, str], records: Sequence[Mapping[str, Any]], *, objective: str, how: str, fixed: str,
                            payload: Mapping[str, Any]) -> list[dict[str, str]]: ...
    def parse_proposal(self, text: Any) -> dict[str, str]: ...


def load_json(path: Path | str) -> tuple[Any, Path]:
    """Read a JSON file named by a person or an agent; errors become :class:`JobError`."""
    resolved = Path(path).expanduser().resolve()
    try:
        return json.loads(resolved.read_text(encoding="utf-8-sig")), resolved
    except OSError:
        raise JobError(f"No se pudo leer {resolved}.", "file-unreadable") from None
    except ValueError:
        raise JobError(f"{resolved} no contiene JSON válido.", "invalid-json") from None


def request_path(value: Any, base_dir: Path | str | None, what: str, code: str) -> Path:
    """A path a request gives, relative to the folder of the request or spec file it came from; ``what`` names it in messages."""
    if not isinstance(value, str) or not value.strip():
        raise JobError(f"'{what}' debe ser una ruta.", code)
    path = Path(value).expanduser()
    if not path.is_absolute():
        if base_dir is None:
            raise JobError(f"'{what}' debe ser absoluta cuando la especificación no viene de un archivo.", code)
        path = Path(base_dir) / path
    return path


def artifact_path(raw: Mapping[str, Any], base_dir: Path | str | None) -> Path:
    """``artifact.path``, relative to the folder of the request or spec file it came from."""
    return request_path(raw["path"], base_dir, "artifact.path", "invalid-artifact")


def _summary(record: Mapping[str, Any]) -> dict[str, Any]:
    artifact = record["artifact"]
    return {"type": artifact["type"], "sha256": record["artifactSha256"], "source": artifact["source"]}


class Kind(Protocol):
    """What the engine needs from an artifact type, apart from running and scoring its cases."""

    @property
    def type(self) -> str: ...
    @property
    def noun(self) -> str: ...  # how messages name the artifact
    @property
    def default_name(self) -> str: ...  # a job's name when its spec gives none
    @property
    def form(self) -> str: ...  # how a request gives the artifact
    @property
    def view_key(self) -> str: ...  # what job views and reviews call a candidate's artifact

    def load(self, raw: Mapping[str, Any], base_dir: Path | str | None) -> tuple[Any, str | None]: ...
    def surface(self, document: Any) -> Surface: ...
    def adapter(self, document: Any, objective: str, *, executor: Any = None, evaluator: Any = None, workspaces: Path | None = None) -> Adapter: ...
    def deduced(self, *, executor: Any = None, evaluator: Any = None) -> dict[str, Any]: ...  # what a template deduces without an original
    def sealed(self, record: Mapping[str, Any]) -> dict[str, Any]: ...
    def view(self, record: Mapping[str, Any]) -> dict[str, Any]: ...
    def export(self, original: Any, candidate: Any) -> tuple[dict[str, str], dict[str, Any]]: ...
    def over_original(self, target: Path, source: Path) -> bool: ...


class PolicyKind:
    """A JEV ``choice`` policy: a JSON file or inline JSON, decided by one call per case."""

    type: ClassVar[str] = JevChoiceAdapter.artifact_type
    noun: ClassVar[str] = "la política"
    default_name: ClassVar[str] = "Política JEV"
    form: ClassVar[str] = "{\"type\": \"jev-policy\"} con 'path' a un archivo JSON o con 'policy' en línea (uno de los dos)"
    view_key: ClassVar[str] = "policy"

    def load(self, raw: Mapping[str, Any], base_dir: Path | str | None) -> tuple[Any, str | None]:
        """The policy document and the file it was read from (``None`` when given inline)."""
        if set(raw) - {"type", "path", "policy"} or ("path" in raw) == ("policy" in raw):
            raise JobError(FORMS, "invalid-artifact")
        if "policy" in raw:
            return raw["policy"], None
        path = artifact_path(raw, base_dir)
        try:
            document, resolved = load_json(path)
        except JobError as error:
            raise JobError(str(error), "artifact-unreadable" if error.code == "file-unreadable" else "invalid-policy") from None
        return document, str(resolved)

    def surface(self, document: Any) -> Surface:
        """The instructions and criteria of the policy ``document``."""
        try:
            return PolicySurface(parse_policy(document))
        except PolicyError as error:
            raise JobError(str(error), "invalid-policy") from None

    def adapter(self, document: Any, objective: str, *, executor: Any = None, evaluator: Any = None, workspaces: Path | None = None) -> Adapter:
        self._settings(executor, evaluator)
        try:
            return cast(Adapter, JevChoiceAdapter(parse_policy(document), objective=objective))
        except PolicyError as error:
            raise JobError(str(error), "invalid-policy") from None

    @staticmethod
    def _settings(executor: Any, evaluator: Any) -> None:
        """A policy takes no executor or evaluator settings."""
        if executor is not None:
            raise JobError("'executor' solo se indica para una Skill: el decisor JEV es una llamada por caso.", "invalid-executor")
        if evaluator is not None:
            raise JobError("'evaluator' solo se indica para una Skill: una política JEV se puntúa con exact-choice (la opción elegida frente a la esperada).",
                           "invalid-evaluator")

    def deduced(self, *, executor: Any = None, evaluator: Any = None) -> dict[str, Any]:
        """The built-in adapter, its evaluator and the case fields a template for policies gets; they do not depend on the policy."""
        self._settings(executor, evaluator)
        return {"adapter": dict(JEV_ADAPTER), "evaluator": dict(JEV_EVALUATOR), "fields": sorted(JevChoiceAdapter.case_fields)}

    def sealed(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """A policy seals no executor or evaluator settings: the decider and exact-choice are fixed."""
        return {}

    def view(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """The policy's question and criteria, for the person who reviews the dataset."""
        original = record["artifact"]["original"]
        return {"artifact": {**_summary(record), "question": original["question"]}, "criteria": original["criteria"]}

    def export(self, original: Any, candidate: Any) -> tuple[dict[str, str], dict[str, Any]]:
        """The candidate policy as ``policy.json``."""
        return {}, {"policy.json": candidate}

    def over_original(self, target: Path, source: Path) -> bool:
        """Exporting to the folder of the original policy file could replace it."""
        return target == source.parent


class SkillKind:
    """A Skill folder: ``SKILL.md`` plus text resources, run in isolated workspaces."""

    type: ClassVar[str] = SkillSessionAdapter.artifact_type
    noun: ClassVar[str] = "la Skill"
    default_name: ClassVar[str] = "Skill"
    form: ClassVar[str] = "{\"type\": \"skill\", \"path\": \"<carpeta con SKILL.md>\"}"
    view_key: ClassVar[str] = "skill"

    def load(self, raw: Mapping[str, Any], base_dir: Path | str | None) -> tuple[Any, str | None]:
        """The Skill's files and the folder they were read from."""
        if set(raw) != {"type", "path"}:
            raise JobError(FORMS, "invalid-artifact")
        folder = artifact_path(raw, base_dir)
        try:
            return load_folder(folder), str(folder.resolve())
        except OSError:
            raise JobError(f"No se pudo leer la carpeta de la Skill {folder}: debe existir y contener SKILL.md.", "artifact-unreadable") from None
        except SkillError as error:
            raise JobError(str(error), "invalid-skill") from None

    def surface(self, document: Any) -> Surface:
        """The ``SKILL.md`` of the Skill ``document``, next to its frozen resources."""
        return SkillSurface(self._skill(document))

    @staticmethod
    def _skill(document: Any) -> Skill:
        try:
            return parse_skill(document)
        except SkillError as error:
            raise JobError(str(error), "invalid-skill") from None

    def adapter(self, document: Any, objective: str, *, executor: Any = None, evaluator: Any = None, workspaces: Path | None = None) -> Adapter:
        """The isolated-session adapter; ``executor`` and ``evaluator`` are the session and evaluator settings a draft seals."""
        skill = self._skill(document)
        session, judge = self._settings(executor, evaluator)
        return cast(Adapter, SkillSessionAdapter(skill, objective=objective, session=session, judge=judge, workspaces=workspaces))

    @staticmethod
    def _settings(executor: Any, evaluator: Any) -> tuple[dict[str, int], JudgeSettings | None]:
        try:
            session = parse_session(executor)
        except SkillError as error:
            raise JobError(str(error), "invalid-executor") from None
        try:
            return session, parse_evaluator(evaluator)
        except EvaluatorError as error:
            raise JobError(str(error), "invalid-evaluator") from None

    def deduced(self, *, executor: Any = None, evaluator: Any = None) -> dict[str, Any]:
        """The built-in adapter, the evaluator with its rule and the case fields that a template's settings give, whatever the Skill."""
        session, judge = self._settings(executor, evaluator)
        adapter = SkillSessionAdapter(Skill(files=()), session=session, judge=judge)  # no Skill: the evaluator and the fields come from the settings only
        return {"adapter": dict(SKILL_ADAPTER), "evaluator": adapter.evaluator(), "fields": sorted(adapter.case_fields)}

    def sealed(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """The session and evaluator settings a draft, an approval or a manifest sealed, as keywords of :meth:`adapter`."""
        return {"executor": sealed_session(record["fixedContract"]), "evaluator": sealed_evaluator(record["evaluator"])}

    def view(self, record: Mapping[str, Any]) -> dict[str, Any]:
        """The Skill's name and files and, with the built-in isolated session, the executor settings the approval seals."""
        fixed = record["fixedContract"]
        return {"artifact": {**_summary(record), "name": fixed["skill"], "files": list(record["artifact"]["original"]["files"])},
                **({"executor": fixed["session"]} if "session" in fixed else {})}

    def export(self, original: Any, candidate: Any) -> tuple[dict[str, str], dict[str, Any]]:
        """A ``skill/`` folder: the candidate ``SKILL.md`` next to the frozen resources."""
        return {f"{SKILL_DIR}/{path}": text for path, text in sorted({**original["files"], **candidate}.items())}, {}

    def over_original(self, target: Path, source: Path) -> bool:
        """Anywhere inside the Skill's folder is where the original lives."""
        return target.is_relative_to(source)


KINDS: Mapping[str, Kind] = MappingProxyType({kind.type: kind for kind in (PolicyKind(), SkillKind())})
FORMS = f"'artifact' debe ser {', o '.join(kind.form for kind in KINDS.values())}."
