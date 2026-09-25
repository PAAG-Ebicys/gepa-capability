"""Versioned optimization jobs: the one contract the CLI, the MCP tools and the skills share.

A job freezes its manifest (original artifact, sealed dataset, adapter, evaluator, mutable
surface, models and limits) before any model call. Running it searches with real GEPA on the
train and validation partitions only, freezes the selection on validation, and only then
evaluates the original and up to five finalists on the reserved test partition. The CLI and
the MCP server translate requests to :class:`JobService`; they never search or score on their own.
"""

from __future__ import annotations

import hashlib
import importlib.metadata
import importlib.util
import math
import os
import platform
import subprocess
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any, Protocol, TypeGuard, cast
from uuid import uuid4

from . import __version__, custom, datasets, declaration, importing, lifecycle, templates
from .artifacts import FORMS, KINDS, Adapter, Kind, request_path
from .config import (ConfigError, Settings, credential_lookup, default_home, has_credential, load_settings, make_connection, open_secret_store, project_root,
                     resolved_folders, write_json_atomic)
from .encoding import storable
from .errors import ContractError, JobError, Stop
from .evaluation import JUDGE_ROLE, JudgeError
from .providers import ModelGateway, ProviderError
from .review import ROLE_NAMES, ReviewError, build as build_review, stored_artifact
from .storage import Storage

JOB_FORMAT = "gepa-job-v1"
EXPORT_FORMAT = "gepa-export-v1"
SPEC_KEYS = frozenset({"name", "artifact", "dataset", "models", "limits", "seed", "objective"})
# ``executor`` and ``evaluator``: a Skill's session and evaluator; ``adapter``: the folder of an adapter an agent prepared; ``template``: the folder of a
# template that gives those three instead. All sealed with the approval.
PREPARE_KEYS = frozenset({"artifact", "objective", "inputs", "split", "executor", "evaluator", "adapter", "template"})
LIMIT_KEYS = frozenset({"maxMetricCalls", "maxProposals", "timeLimitMinutes", "reflectionMinibatchSize"})  # plus each adapter's output tokens per role
MAX_OUTPUT_TOKENS = 32768
PREVIEW_SAMPLE = 6  # original runs shown before approving
REVIEW_RECOMMENDATION = ("Recomendación firme: revisa el dataset completo antes de aprobar (cada caso, su partición y su 'expected'). "
                         "El resumen y la muestra del original no bastan para detectar etiquetas erróneas o casos que no representan el objetivo.")
MAX_PREVIEW_SAMPLE = 200
PREVIEW_TIMEOUT_SECONDS = 120.0
MAX_FINALIST_PROPOSALS = 5  # finalists are the original plus at most five proposals
MAX_PROPOSAL_KEPT = 8000  # characters of each reflection answer kept as evidence
PROGRESS_SAVE_SECONDS = 1.0  # per-case progress is persisted at most this often; milestones always are
JOB_FILES = "jobs"  # folder of the data folder with each job's runner lock, the log of its detached process and the folder of its search
SEARCH_FILES = "gepa"  # a job's search folder, <datos>/jobs/<jobId>/gepa: where GEPA saves its state at the start of each iteration
GEPA_STATE = "gepa_state.bin"
SEARCH_STATE_FORMAT = "gepa-search-state-v1"  # what the engine keeps in each GEPA state it saves, to tie the state to its job
# Modules whose code decides what a run does, plus every module jobs.py imports (review.py only reads finished
# evidence, but a module loaded with the run is fingerprinted with it); any change yields a different fingerprint.
# An approval holds only for its own adapter's modules (``Adapter.modules``), the code that ran its preview.
FINGERPRINTED_MODULES = ("artifacts.py", "config.py", "custom.py", "datasets.py", "declaration.py", "encoding.py", "errors.py", "evaluation.py", "files.py", "importing.py", "jev.py",
                         "jobs.py", "lifecycle.py", "providers.py", "review.py", "skill.py", "spaces.py", "storage.py", "templates.py")
SAMPLING_NOTE = "Valores por defecto del proveedor: el motor no fija temperatura ni semilla del modelo."

Launcher = Callable[[str], None]


class Gateway(Protocol):
    def complete(self, connection: dict, messages: list[dict], max_tokens: int = ..., timeout: float = ...) -> dict: ...


class _SilentLogger:
    def log(self, message: str) -> None:
        """GEPA logs candidate text and provider errors: keep them off stdout (MCP) and out of records."""


_now = lifecycle.now
_event = lifecycle.event


def _public_error(error: BaseException) -> dict[str, str]:
    if isinstance(error, (ProviderError, JobError, JudgeError)):
        return {"code": error.code, "message": str(error)}
    if isinstance(error, KeyboardInterrupt):
        return {"code": "interrupted", "message": "El proceso se interrumpió; la evidencia parcial se conservó."}
    return {"code": "internal-error", "message": f"El trabajo se detuvo por un error interno ({type(error).__name__}); la evidencia parcial se conservó."}


def error_payload(error: Exception) -> dict[str, Any]:
    """The structured error both the CLI (``--json``) and the MCP tools return: its message, its code and the details it carries."""
    code = error.code if isinstance(error, (JobError, ProviderError)) else "invalid-configuration" if isinstance(error, ConfigError) else "invalid-request"
    return {"error": str(error), "code": code, **(error.details if isinstance(error, JobError) else {})}


def _engine_version() -> str:
    return importlib.metadata.version("gepa")


def _code_fingerprint(modules: tuple[str, ...] = FINGERPRINTED_MODULES) -> str:
    """SHA-256 of the engine modules that execute a job: a manual version number would miss code changes."""
    root = Path(__file__).resolve().parent
    digest = hashlib.sha256()
    for name in modules:
        digest.update(name.encode() + b"\0" + (root / name).read_bytes() + b"\0")
    return digest.hexdigest()


def _finite(value: Any) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _integer(raw: Mapping[str, Any], key: str, default: int, low: int, high: int, code: str = "invalid-limits") -> int:
    value = raw.get(key, default)
    if not _finite(value) or int(value) != value or not low <= value <= high:
        raise JobError(f"'{key}' debe ser un entero entre {low} y {high}.", code)
    return int(value)


def _added_usage(totals: Mapping[str, Any], usage: Any) -> dict[str, Any]:
    """Token totals plus one answer's counts; a count the provider did not report makes that total unknown, never a silent zero."""
    reported = usage if isinstance(usage, dict) else {}
    return {key: total + reported[key] if total is not None and _finite(reported.get(key)) else None for key, total in totals.items()}


def _known_cost(value: Any) -> TypeGuard[int | float]:
    return _finite(value) and value >= 0


def _number(value: float) -> int | float:
    """A sum of case scores: whole when every score was, fractional otherwise (never truncated)."""
    return int(value) if float(value).is_integer() else value


class _Meter:
    """Calls, tokens and cost of a short series of model calls, such as a preview."""

    def __init__(self) -> None:
        self.calls = 0
        self.usage: dict[str, Any] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        self.cost: float | None = 0.0

    def add(self, answer: Mapping[str, Any]) -> Mapping[str, Any]:
        self.usage = _added_usage(self.usage, answer.get("usage"))
        cost = answer.get("costUsd")
        self.cost = self.cost + cost if self.cost is not None and _known_cost(cost) else None
        return answer

    def lost(self) -> None:
        """A failed or interrupted call may have been billed: totals become unknown."""
        self.usage = dict.fromkeys(self.usage)
        self.cost = None

    def report(self) -> dict[str, Any]:
        return {"counts": {"modelCalls": self.calls}, "usage": self.usage, "costUsd": self.cost if self.calls else None}


def _iteration_cost(minibatch: int, validation: int) -> int:
    """Evaluations of one accepted GEPA iteration: parent and child on a minibatch, then full validation of the child."""
    return 2 * minibatch + validation


def _candidate_id(texts: Mapping[str, str]) -> str:
    return "cand-" + datasets.sha256_of(dict(texts))[:12]


def _measurement(total: int) -> dict[str, Any]:
    """Score of one candidate on one partition; ``score`` stays ``None`` until every case is in."""
    return {"status": "pending", "score": None, "correct": 0, "total": total, "cases": []}


def _complete(measurement: dict[str, Any], results: list[dict[str, Any]]) -> None:
    correct = math.fsum(result["score"] for result in results)  # exact: three cases at 2/3 and one at 1 add up to 3
    measurement.update(status="complete", score=correct / len(results), correct=_number(correct))


def _summary(measurement: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if measurement is None:
        return None
    return {key: measurement[key] for key in ("status", "score", "correct", "total")}


def _objective(value: Any) -> str:
    if not isinstance(value, str) or len(value) > 4000:
        raise JobError("'objective' debe ser texto de hasta 4000 caracteres.")
    return value


def _kind(kind: Any) -> Kind:
    """The kind of an artifact type; an unknown type is a :class:`JobError`."""
    if kind not in KINDS:
        raise JobError(f"Tipo de artefacto no admitido: {kind!r}; usa {', '.join(repr(name) for name in KINDS)}.", "invalid-artifact")
    return KINDS[kind]


def _verified(adapter: Adapter) -> Adapter:
    """The adapter, once its declared requirements (for a Skill, a workspace it can create) hold; before any model call."""
    adapter.verify_requirements()
    return adapter


def _dataset_view(record: Mapping[str, Any], *, cases: bool = False) -> dict[str, Any]:
    """What a person reviews before approving, and what an approval sealed: objective, coverage, criteria, problems and baseline preview."""
    sealed = record["format"] == datasets.SEALED_FORMAT
    view = {
        "kind": "dataset" if sealed else "draft", "draftId": record["draftId"] if sealed else record["id"],
        "datasetId": record["id"] if sealed else record["approvedAs"], "ready": record["id"] is not None,
    }
    if sealed:
        view.update({key: record[key] for key in ("sha256", "casesSha256", "approvedAt", "reviewedAllCases")})
    view |= {
        "objective": record["objective"], **({"template": record["template"]} if "template" in record else {}),  # the template version it was prepared from
        **_kind(record["artifact"]["type"]).view(record), "adapter": record["adapter"],
        **({"adapterSource": record["adapterSource"]} if "adapterSource" in record else {}),  # the folder an agent's adapter was read from
        "evaluator": record["evaluator"], "fixedContract": record["fixedContract"], "mutableSurface": record["mutableSurface"],
        "adapterCodeSha256": record["adapterCodeSha256"], "split": record["split"],
        "splits": datasets.partitions(record["cases"]), "coverage": datasets.coverage(record["cases"], tuple(record["fixedContract"].get("options", ()))),
        "issues": record["issues"], "preview": record["preview"], "preparedAt": record["preparedAt"],
        "review": {"recommendation": REVIEW_RECOMMENDATION, "fullDataset": None if record["id"] is None else {
            "cli": f"gepa dataset show {record['id']} --cases", "tool": {"name": "gepa_dataset_show", "arguments": {"datasetId": record["id"], "cases": True}}}},
    }
    view["counts"] = {split: len(members) for split, members in view["splits"].items()}
    view["next"] = _next_step(view)
    return {**view, "cases": record["cases"]} if cases else view


def _next_step(view: Mapping[str, Any]) -> str:
    preview = view["preview"]
    if not view["ready"]:
        return "Corrige los errores de 'issues' en los casos o en su origen y vuelve a preparar."
    if view["datasetId"]:
        return f"Aprobado: usa \"dataset\": \"{view['datasetId']}\" en la especificación del trabajo."
    if preview is None or preview["status"] != "complete":
        return f"Ejecuta el original sobre algunos casos antes de aprobar: gepa dataset preview {view['draftId']}"
    return f"Tras revisar el dataset completo, aprueba con: gepa dataset approve {view['draftId']} [--reviewed-all]"


class JobService:
    def __init__(self, settings: Settings, *, gateway: Gateway | None = None, launcher: Launcher | None = None, environ: Mapping[str, str] | None = None) -> None:
        self.settings = settings
        self.store = Storage(settings.data_dir)
        self.workspaces = settings.data_dir / "workspaces"  # where Skill cases run, one new folder per evaluation
        self.adapters = declaration.Store(settings.data_dir / "adapters")  # frozen copies of the adapters agents prepared
        self.template_copies = templates.Store(settings.data_dir / "templates")  # frozen copies of the templates drafts were prepared with
        self.job_files = settings.data_dir / JOB_FILES
        self.gateway = gateway
        self.launcher = launcher
        self.environ = os.environ if environ is None else environ
        # An agent's adapter reads its credentials from this environment, and never passes on a connection's credential to a command it runs.
        self.runtime = custom.Runtime(workspaces=self.workspaces, environ=self.environ,
                                      hidden=frozenset(connection.api_key_env for connection in settings.connections if connection.api_key_env))

    @classmethod
    def for_home(cls, home: Path | None, *, gateway: Gateway | None = None, launcher: Launcher | None = None) -> JobService:
        """The service the CLI and the MCP server share; background runs go to a separate process by default."""
        home = home or default_home()
        settings = load_settings(home)
        return cls(settings, gateway=gateway, launcher=launcher or detached_launcher(home, settings.data_dir))

    # Datasets -----------------------------------------------------------------------------

    def prepare(self, request: Any, *, base_dir: Path | str | None = None) -> dict[str, Any]:
        """Import, validate and partition cases for an artifact; store a draft when nothing blocks it. No model is called."""
        if not isinstance(request, dict):
            raise JobError("La solicitud de preparación debe ser un objeto JSON.")
        unknown = set(request) - PREPARE_KEYS
        if unknown:
            raise JobError(f"Campos no admitidos en la preparación: {', '.join(sorted(unknown))}.")
        objective = request.get("objective")
        if not isinstance(objective, str) or not objective.strip():
            raise JobError("'objective' es obligatorio: en una o dos frases, qué comportamiento debe mejorar y qué debe protegerse.")
        objective = _objective(objective.strip())
        kind, document, source = self._artifact(request.get("artifact"), base_dir)
        template: dict[str, Any] | None = None
        if request.get("template") is None:
            adapter, adapter_source = self._requested_adapter(kind, document, objective, request, base_dir)
        else:
            (template, adapter), adapter_source = self._applied_template(kind, document, objective, request, base_dir), None
        try:
            raw = importing.load_inputs(request.get("inputs"), base_dir)
        except importing.InputError as error:
            raise JobError(str(error), "invalid-input") from None
        split = request.get("split", {})
        if not isinstance(split, dict) or set(split) - {"seed"}:
            raise JobError("'split' admite solo 'seed', la semilla de la división de los casos sin partición.")
        seed = _integer(split, "seed", datasets.DEFAULT_SEED, 0, 2**31 - 1, "invalid-request")
        prepared = datasets.prepare(raw, check_case=adapter.check_case, used_fields=adapter.case_fields, labels=adapter.labels, seed=seed)
        original = adapter.original()
        content = {"format": datasets.DRAFT_FORMAT, "artifactSha256": datasets.sha256_of(original), **adapter.describe(),
                   "adapterCodeSha256": _code_fingerprint(adapter.modules), "objective": objective, "split": prepared.split, "cases": prepared.cases,
                   **({"template": template} if template else {})}
        draft_id = "draft-" + datasets.sha256_of(content)[:16]
        record = {"id": draft_id, **content, "artifact": {"type": adapter.artifact_type, "source": source, "original": original},
                  **({"adapterSource": adapter_source} if adapter_source else {}), "issues": prepared.issues, "preparedAt": _now(), "preview": None, "approvedAs": None}
        if not prepared.ready:
            return _dataset_view({**record, "id": None})
        # The same content is the same draft: an earlier preview or approval of it is kept, and the adapter's folder is where it was read now.
        # A template's application id makes each application new content.
        if not self.store.insert("draft", draft_id, record) and adapter_source:
            self.store.update("draft", draft_id, lambda current: {**current, "adapterSource": adapter_source})
        return _dataset_view(self.store.get("draft", draft_id))

    def preview(self, draft_id: str, *, sample: int | None = None, decider: str | None = None, judge: str | None = None) -> dict[str, Any]:
        """Run the original artifact on representative training and validation cases, so a person checks the evaluator before approving.

        The sample covers every expected value (by default at least ``PREVIEW_SAMPLE`` cases); reserved
        test cases never enter it. ``decider`` and ``judge`` name other connections for those roles. The
        result is stored on the draft; approving requires a complete one.
        """
        draft = self._draft(draft_id)
        adapter = self._stored_adapter(draft)
        stratified = bool(adapter.labels)
        minimum = datasets.preview_minimum(draft["cases"], stratified=stratified)
        sample = max(PREVIEW_SAMPLE, minimum) if sample is None else sample
        if not _finite(sample) or int(sample) != sample or not minimum <= sample <= MAX_PREVIEW_SAMPLE:
            covered = "cada opción con casos de train y val" if stratified else "casos de train y de val"
            raise JobError(f"'sample' debe cubrir {covered}: un entero entre {minimum} y {MAX_PREVIEW_SAMPLE}.")
        _require_current_adapter(draft, adapter)
        role = adapter.executor_role
        # Every role a case needs (the executor and, in a judged evaluation, the judge) is resolved before any model call.
        case_roles = {name: target for name, target in adapter.roles.items() if name != "reflection"}
        if judge is not None and JUDGE_ROLE not in case_roles:
            raise JobError("Este borrador se evalúa sin juez: 'judge' solo se indica con un evaluador que lo consulta (rubric-judge, o un adaptador que declara el rol judge).",
                           "invalid-models")
        overrides = {key: value for key, value in ((role, decider), (JUDGE_ROLE, judge)) if value is not None}
        models = self._models(overrides, roles=case_roles)
        request = self._requester({"models": models})
        meter = _Meter()

        def metered(role: str, messages: list[dict[str, str]], max_tokens: int | None = None) -> Mapping[str, Any]:
            meter.calls += 1
            try:
                return meter.add(request(role, messages, max_tokens=max_tokens or adapter.preview_max_tokens, timeout=PREVIEW_TIMEOUT_SECONDS))
            except BaseException:
                meter.lost()
                raise

        chosen = datasets.preview_sample(draft["cases"], int(sample), draft["split"].get("seed", datasets.DEFAULT_SEED), stratified=stratified)
        _verified(adapter)
        preview: dict[str, Any] = {"status": "running", "startedAt": _now(), "finishedAt": None, "role": role, "model": models[role], "models": models,
                                   "sample": [case["id"] for case in chosen], "cases": [], "score": None, "error": None}
        try:
            for case in chosen:
                preview["cases"].append(adapter.run_case(adapter.seed(), case, metered))
            correct = math.fsum(result["score"] for result in preview["cases"])
            preview.update(status="complete", score={"correct": _number(correct), "total": len(chosen), "score": correct / len(chosen)})
        except Exception as error:
            preview.update(status="failed", error=_public_error(error))
        preview.update(finishedAt=_now(), **meter.report())
        stored = self.store.update("draft", draft_id, lambda record: {**record, "preview": preview})
        return {"draftId": draft_id, **preview, "next": _next_step(_dataset_view(stored))}

    def approve(self, draft_id: str, *, reviewed_all_cases: bool = False) -> dict[str, Any]:
        """Seal a previewed draft: its cases bound to the artifact, the adapter, the evaluator and the code that ran the preview.

        The sealed version is immutable; approving the same draft again returns it unchanged.
        """
        draft = self._draft(draft_id)
        _require_current_adapter(draft, self._stored_adapter(draft))
        preview = draft["preview"]
        if preview is None:
            raise JobError(f"Antes de aprobar, ejecuta el original sobre algunos casos: 'gepa dataset preview {draft_id}'.", "preview-missing")
        if preview["status"] != "complete":
            raise JobError(f"La última previsualización no se completó ({preview['error']['code']}); repítela antes de aprobar.", "preview-failed")
        sha = datasets.seal_sha256(draft)
        record = {**draft, "format": datasets.SEALED_FORMAT, "id": "ds-" + sha[:16], "sha256": sha, "casesSha256": datasets.sha256_of(draft["cases"]),
                  "draftId": draft_id, "approvedAt": _now(), "reviewedAllCases": bool(reviewed_all_cases)}
        record["approvedAs"] = record["id"]
        self.store.insert("dataset", record["id"], record)
        self.store.update("draft", draft_id, lambda current: {**current, "approvedAs": record["id"]})
        return _dataset_view(self.store.get("dataset", record["id"]))

    def dataset(self, dataset_id: str, *, cases: bool = False) -> dict[str, Any]:
        """A draft or an approved version; ``cases=True`` includes every case, for a complete review."""
        if isinstance(dataset_id, str) and dataset_id.startswith("draft-"):
            return _dataset_view(self._draft(dataset_id), cases=cases)
        return _dataset_view(self._sealed(dataset_id), cases=cases)

    def _sealed(self, dataset_id: Any) -> dict[str, Any]:
        record = self.store.get("dataset", dataset_id) if isinstance(dataset_id, str) else None
        if record is None:
            if isinstance(dataset_id, str) and self.store.get("draft", dataset_id) is not None:
                raise JobError(f"'{dataset_id}' es un borrador sin aprobar: previsualiza el original y apruébalo con 'gepa dataset approve {dataset_id}'.", "dataset-not-approved")
            raise JobError(f"El dataset '{dataset_id}' no está aprobado; prepáralo y apruébalo con 'gepa dataset prepare' y 'gepa dataset approve'.", "dataset-not-approved")
        if record.get("format") != datasets.SEALED_FORMAT:
            raise JobError(f"El dataset '{dataset_id}' se aprobó sin previsualización ni evaluador sellado (formato anterior); prepáralo y apruébalo de nuevo.", "dataset-not-approved")
        return cast(dict[str, Any], record)

    # Jobs ---------------------------------------------------------------------------------

    def create(self, spec: Any, *, base_dir: Path | str | None = None) -> dict[str, Any]:
        """Validate the request and freeze the manifest. No model is called."""
        if not isinstance(spec, dict):
            raise JobError("La especificación del trabajo debe ser un objeto JSON.")
        unknown = set(spec) - SPEC_KEYS
        if unknown:
            raise JobError(f"Campos no admitidos en la especificación: {', '.join(sorted(unknown))}.")
        # The artifact is the policy under optimization; its option IDs are content, not credential fields.
        secret = datasets.find_secret_key({key: value for key, value in spec.items() if key != "artifact"})
        if secret:
            raise JobError(f"El campo '{secret}' parece una credencial: las credenciales solo se configuran en las conexiones.", "secret-in-request")
        if importlib.util.find_spec("gepa") is None:
            raise JobError("El motor GEPA no está instalado; ejecuta 'gepa setup check' para ver cómo instalarlo.", "engine-missing")
        kind, document, source = self._artifact(spec.get("artifact"), base_dir)
        name = spec.get("name", kind.default_name)
        if not isinstance(name, str) or not name.strip() or len(name) > 200:
            raise JobError("'name' debe ser texto de hasta 200 caracteres.")
        record = self._sealed(spec.get("dataset"))
        objective = record["objective"]
        if "objective" in spec and _objective(spec["objective"]).strip() != objective:
            raise JobError("El objetivo del trabajo no es el aprobado con este dataset; omítelo para usar el aprobado, o prepara y aprueba el dataset con el nuevo.", "objective-not-approved")
        what = _kind(record["artifact"]["type"]).noun
        if kind.type != record["artifact"]["type"]:
            raise JobError(f"Este dataset se aprobó con {what}, no con un artefacto de tipo {kind.type!r}; prepara y aprueba un dataset para este artefacto.", "artifact-not-approved")
        adapter = self._sealed_adapter(kind, document, objective, record)
        original = adapter.original()
        if datasets.sha256_of(original) != record["artifactSha256"]:
            raise JobError(f"{what[0].upper()}{what[1:]} no es la que se previsualizó y aprobó con este dataset; prepara y aprueba el dataset con esta versión.", "artifact-not-approved")
        _require_current_adapter(record, adapter)
        for case in record["cases"]:
            try:
                adapter.validate_case(case)
            except ContractError as error:
                raise JobError(str(error), "invalid-dataset") from None
        splits = datasets.partitions(record["cases"])
        counts = {split: len(members) for split, members in splits.items()}
        models = self._models(spec.get("models"), roles=adapter.roles)
        limits = self._limits(spec.get("limits"), counts, adapter)
        _verified(adapter)
        manifest = {
            "format": JOB_FORMAT,
            "artifact": {"type": adapter.artifact_type, "source": source, "original": original, "sha256": datasets.sha256_of(original)},
            **adapter.describe(),
            "dataset": {"id": record["id"], "sha256": record["sha256"], "casesSha256": record["casesSha256"], "counts": counts, "splits": splits,
                        "draftId": record["draftId"], "approvedAt": record["approvedAt"], "reviewedAllCases": record["reviewedAllCases"]},
            "models": models,
            "limits": limits,
            "seed": _integer(spec, "seed", 42, 0, 2**31 - 1),
            "objective": objective,
            **({"template": record["template"]} if "template" in record else {}),  # its presentation is the review's, whatever later versions say
            "engine": {"gepa": _engine_version(), "capability": __version__, "codeSha256": _code_fingerprint()},
            "execution": {**adapter.execution, "platform": platform.platform(), "python": platform.python_version()},
        }
        now = _now()
        job: dict[str, Any] = {
            "id": "job-" + uuid4().hex[:12], "name": name.strip(), "status": "queued", "phase": None,
            "createdAt": now, "startedAt": None, "updatedAt": now, "finishedAt": None,
            "manifest": manifest, "manifestSha256": datasets.sha256_of(manifest), "result": None, "events": [], "error": None,
            "attempts": [lifecycle.new_attempt(1, "run", now)],
        }
        role = adapter.executor_role
        _event(job, "created", f"Manifiesto congelado ({job['manifestSha256'][:12]}): dataset {record['id']}, {ROLE_NAMES.get(role, role)} {manifest['models'][role]['model']}.")
        self.store.put("job", job["id"], job)
        return self._view(job)

    def start(self, spec: Any, *, base_dir: Path | str | None = None, detach: bool = False) -> dict[str, Any]:
        """Create a job and run it here, or hand it to the launcher and return while it is queued."""
        if detach and self.launcher is None:
            raise JobError("No hay un lanzador configurado para ejecutar en segundo plano.", "launcher-missing")
        view = self.create(spec, base_dir=base_dir)
        if not detach:
            return self.run(view["jobId"])
        self._launch(view["jobId"])
        return view

    def run(self, job_id: str) -> dict[str, Any]:
        """Execute a queued attempt to the end: the whole job, or a retry's pending reserved test. Failures are recorded on the job, not raised.

        The runner holds the job's lock from before it claims the job until it closes it, so a reader can tell it is still alive.
        """
        if not isinstance(job_id, str) or self.store.get("job", job_id) is None:
            raise JobError(f"No existe el trabajo '{job_id}'.", "job-not-found")
        lock = lifecycle.RunnerLock(self._lock_path(job_id))
        if not lock.acquire():
            raise JobError(f"El trabajo {job_id} ya se está ejecutando en otro proceso.", "job-not-queued")
        try:
            return self._run_claimed(job_id)
        finally:
            lock.release()

    def _run_claimed(self, job_id: str) -> dict[str, Any]:
        def claim(job: dict[str, Any]) -> dict[str, Any]:
            if job["status"] != "queued":
                raise JobError(f"El trabajo {job_id} está en estado '{job['status']}'; solo se ejecuta un trabajo en cola.", "job-not-queued")
            now = _now()
            attempt = lifecycle.attempt_of(job)
            job.update(status="running", phase="preparation", startedAt=job["startedAt"] or now, updatedAt=now, finishedAt=None, error=None)
            attempt.update(status="running", phase="preparation", startedAt=now)
            _event(job, "started", f"Intento {attempt['attempt']} iniciado: se comprueban el dataset sellado, el código congelado y los requisitos "
                                   "del adaptador antes de llamar a modelos.")
            return job

        job = self.store.update("job", job_id, claim)

        def persist() -> None:
            job["updatedAt"] = _now()
            self.store.put("job", job_id, job)

        attempt = lifecycle.attempt_of(job)
        number = attempt["attempt"]
        run: _Run | None = None
        try:
            adapter, cases = self._runnable(job["manifest"])
            folder = self._search_folder(job_id)
            saved = _saved_search(job, folder, self.settings.data_dir) if attempt["kind"] == "continue-search" else None  # checked again: it may have changed since the retry
            run = _Run(job, adapter, cases, self._requester(job["manifest"]), persist, cancelled=lambda: lifecycle.cancel_requested(self.store, job_id, number),
                       record_spend=lambda evaluations: lifecycle.record_search_spend(self.store, job_id, evaluations), folder=folder, saved=saved)
            run.execute()
        except BaseException as error:
            if run is not None:
                run.record_seconds()
            status = "interrupted" if isinstance(error, KeyboardInterrupt) else "failed"
            lifecycle.finish(job, status, error=_public_error(error))
            _event(job, status, job["error"]["message"])
            persist()
            if not isinstance(error, Exception):
                raise
        return self._view(job)

    def cancel(self, job_id: str) -> dict[str, Any]:
        """Cancel a job from any session: a queued one at once; a running one at its runner's next safe point, before any new model call.

        Candidates and evidence already stored are kept. Cancelling again returns the same request.
        """
        job = self._job(job_id)
        if job["status"] == "queued":
            def cancel_queued(current: dict[str, Any]) -> dict[str, Any]:
                if current["status"] == "queued":
                    lifecycle.finish(current, "cancelled", reason="cancelled")
                    number = lifecycle.attempt_of(current)["attempt"]
                    _event(current, "cancelled", "Cancelado antes de empezar: no se llamó a ningún modelo." if number == 1 else
                           f"Intento {number} cancelado antes de empezar: no llamó a ningún modelo; lo que gastaron los anteriores sigue contabilizado.")
                return current
            job = self._refreshed(self.store.update("job", job_id, cancel_queued))
        if job["status"] == "running":
            lifecycle.request_cancel(self.store, job_id, lifecycle.attempt_of(job)["attempt"])
        elif job["status"] != "cancelled":
            raise JobError(f"El trabajo {job_id} ya no está en curso ({job['status']}): no hay nada que cancelar.", "job-not-active")
        return self._view(job)

    def retry(self, job_id: str, *, kind: str | None = None, detach: bool = False) -> dict[str, Any]:
        """Retry a job that stopped before completing, as a new attempt of one of the ``kind`` its ``recovery`` offers.

        With the selection frozen, a ``final-retry`` evaluates only the pending reserved test cases. A search cut short can close with a
        ``final-retry`` among the candidates validated so far or, once GEPA saved its state, continue with a ``continue-search``: with two
        recoveries, ``kind`` is required, so the person chooses. Completed results are reused and what earlier attempts spent stays counted.
        The dataset, the frozen code, the adapter's requirements and, to continue, GEPA's saved state are checked before anything is queued.
        """
        job = self._job(job_id)
        plan = lifecycle.recovery(job)
        if plan is None:
            if job["status"] == "completed":
                raise JobError(f"El trabajo {job_id} ya se completó: no hay nada que reintentar. Revisa su evidencia con 'gepa job review {job_id}'.", "job-completed")
            raise JobError(f"El trabajo {job_id} sigue en curso ({job['status']}); consúltalo con 'gepa job show {job_id}' o cancélalo antes.", "job-active")
        if not plan["retryable"]:
            raise JobError(plan["message"], plan["code"])
        kinds = [option["kind"] for option in plan["options"]]
        if kind is None and len(kinds) > 1:
            raise JobError(f"{plan['message']} Indica cuál con: {' o '.join(option['cli'] for option in plan['options'])}.", "recovery-choice-required")
        if kind is not None and kind not in kinds:
            raise JobError(f"Este trabajo no admite la recuperación {kind!r}, sino {' o '.join(map(repr, kinds))}: {plan['message']}", "recovery-not-available")
        if detach and self.launcher is None:
            raise JobError("No hay un lanzador configurado para ejecutar en segundo plano.", "launcher-missing")
        self._runnable(job["manifest"])
        chosen = kind or kinds[0]
        if chosen == "continue-search":
            _saved_search(job, self._search_folder(job_id), self.settings.data_dir)

        def requeue(current: dict[str, Any]) -> dict[str, Any]:
            if lifecycle.recovery(current) != plan:
                raise JobError(f"El trabajo {job_id} cambió mientras se preparaba el reintento; consúltalo de nuevo.", "job-changed")
            attempt = lifecycle.new_attempt(len(current["attempts"]) + 1, chosen, _now())
            current["attempts"].append(attempt)
            current.update(status="queued", phase=None, error=None, finishedAt=None, updatedAt=_now())
            if chosen == "final-retry":
                what = ("prueba reservada de los casos pendientes, sin repetir GEPA" if current["result"]["selection"] else
                        "elegir entre los candidatos ya validados y evaluar la prueba reservada, sin continuar GEPA")
            else:
                what = "continuar la búsqueda GEPA desde su último estado guardado y después la prueba reservada" if chosen == "continue-search" else "trabajo completo"
            _event(current, "retry", f"Intento {attempt['attempt']} solicitado: {what}. Los intentos anteriores y lo que contabilizaron se conservan.")
            return current

        self.store.update("job", job_id, requeue)
        if not detach:
            return self.run(job_id)
        self._launch(job_id)
        return self.show(job_id)

    def show(self, job_id: str, *, cases: bool = False) -> dict[str, Any]:
        return self._view(self._job(job_id), cases=cases)

    def list(self) -> dict[str, Any]:
        return {"jobs": [{"jobId": job["id"], "name": job["name"], "status": job["status"], "phase": job["phase"],
                          "createdAt": job["createdAt"], "finishedAt": job["finishedAt"], "datasetId": job["manifest"]["dataset"]["id"],
                          "selectedId": ((job.get("result") or {}).get("selection") or {}).get("selectedId")}
                         for job in map(self._refreshed, self.store.list("job"))]}

    def review(self, job_id: str, *, candidate_ids: Sequence[str] | None = None, case_id: str | None = None, cases: bool = False) -> dict[str, Any]:
        """Explain a job from its stored evidence: the original against up to five candidates, per case, with a recommendation.

        No model is called and today's settings are not read. Earlier jobs of this data folder tell
        whether the reserved test had already been observed.
        """
        job = self._job(job_id)
        dataset = self.store.get("dataset", job["manifest"]["dataset"]["id"]) or {}
        try:
            return build_review(job, dataset.get("cases", []), self.store.list("job"), artifact_key=_view_key(job["manifest"]),
                                candidate_ids=candidate_ids, case_id=case_id, cases=cases)
        except ReviewError as error:
            raise JobError(str(error), error.code) from None

    def export(self, job_id: str, out_dir: Path | str, *, candidate_id: str | None = None) -> dict[str, Any]:
        """Write the chosen finalist (a policy, or a Skill folder), the manifest and per-case evidence to a new folder.

        Never writes over the original: the target must be a new or empty folder, outside a Skill's own folder.
        """
        job = self._job(job_id)
        result = job.get("result") or {}
        if job["status"] != "completed" or (result.get("finalCheck") or {}).get("status") != "complete":
            raise JobError("El trabajo no completó la prueba reservada; no hay un candidato exportable.", "job-not-completed")
        selection = result["selection"]
        baseline_id = result["baselineId"]
        artifact = job["manifest"]["artifact"]
        kind = _kind(artifact["type"])
        what = kind.noun
        if candidate_id is None:
            if selection["selectedIsOriginal"]:
                raise JobError("Ningún candidato superó al original en validación: conserva el original. Si igualmente quieres un finalista, indícalo con su id.", "original-selected")
            candidate_id = selection["selectedId"]
        if candidate_id == baseline_id:
            raise JobError(f"El original no se exporta como candidato: ya es {what} que usas.", "original-not-exportable")
        if candidate_id not in selection["finalistIds"]:
            raise JobError(f"Solo se exportan finalistas con prueba reservada completa: {', '.join(i for i in selection['finalistIds'] if i != baseline_id) or 'ninguno'}.", "candidate-not-finalist")
        target = Path(out_dir).expanduser().resolve()
        source = Path(artifact["source"]).resolve() if artifact["source"] else None
        if source is not None and kind.over_original(target, source):
            raise JobError(f"La carpeta de exportación contiene {what} original; elige una carpeta nueva.", "export-over-original")
        if target.exists() and (not target.is_dir() or any(target.iterdir())):
            raise JobError(f"{target} ya existe y no está vacía; elige una carpeta nueva.", "export-target-not-empty")
        by_id = {candidate["id"]: candidate for candidate in result["candidates"]}
        candidate, original = by_id[candidate_id], by_id[baseline_id]
        report = self.review(job_id)

        def evidence(item: Mapping[str, Any]) -> dict[str, Any]:
            return {"candidateId": item["id"], "label": item["label"], "validation": item["validation"], "test": item["test"], "search": item["searchCases"]}

        texts, documents = kind.export(artifact["original"], stored_artifact(candidate))
        files: dict[str, Any] = {
            **documents,
            "manifest.json": {
                "format": EXPORT_FORMAT, "jobId": job["id"], "name": job["name"], "candidateId": candidate_id, "label": candidate["label"],
                "baselineId": baseline_id, "exportedCandidateIsSelected": candidate_id == selection["selectedId"], "selection": selection,
                "manifestSha256": job["manifestSha256"], "manifest": job["manifest"], "counts": result["counts"], "usage": result["usage"],
                "costUsd": result["costUsd"], "consumption": lifecycle.consumption(job), "finishedAt": job["finishedAt"],
                "review": {key: report[key] for key in ("format", "recommendation", "testObservedBefore", "limits")},
                "note": f"Exportación explícita: {what} original no se modificó ni se reemplazó.",
            },
            "evidence.json": {"format": EXPORT_FORMAT, "jobId": job["id"], "primaryMetric": job["manifest"]["evaluator"]["primaryMetric"],
                              "candidate": evidence(candidate), "original": evidence(original)},
        }
        try:
            target.mkdir(parents=True, exist_ok=True)
            for name, text in texts.items():
                path = target.joinpath(*name.split("/"))
                path.parent.mkdir(parents=True, exist_ok=True)
                with open(path, "w", encoding="utf-8", newline="") as handle:  # the exact text the job evaluated
                    handle.write(text)
            for name, document in files.items():
                write_json_atomic(target / name, document)
        except OSError:
            raise JobError(f"No se pudo escribir en {target}.", "export-failed") from None
        return {"jobId": job["id"], "candidateId": candidate_id, "selected": candidate_id == selection["selectedId"], "outDir": str(target),
                "files": [*texts, *files]}

    # Adapters -----------------------------------------------------------------------------

    def adapter_init(self, path: Path | str, *, example: str = declaration.DEFAULT_EXAMPLE) -> dict[str, Any]:
        """Copy an example adapter into a new or empty folder, for an agent to adapt to a new task."""
        return declaration.init(Path(path).expanduser().resolve(), example)

    def adapter_check(self, path: Path | str) -> dict[str, Any]:
        """Validate an adapter folder and diagnose what it needs (modules, commands, credentials, its own check), before any model call."""
        return custom.check_report(Path(path).expanduser().resolve(), self.runtime)

    # Templates ----------------------------------------------------------------------------

    def template_save(self, request: Any, *, base_dir: Path | str | None = None) -> dict[str, Any]:
        """Save the class of task of an approved dataset as a new template folder, version 1: its adapter with the executor and evaluator
        settings, the guide to gather its cases and the rules to present results.

        The folder goes to ``path`` (relative to the request's folder) or, by default, to the designated templates folder, named after
        ``name``; an existing folder is never overwritten. An agent's adapter is copied from the approved frozen copy. No case, preview,
        result, model or connection is copied, and no model is called.
        """
        fields = _template_request(request)
        record = self._sealed(fields.get("from"))
        kind = _kind(record["artifact"]["type"])
        # An agent's adapter declares its own executor and evaluator, and the template carries the approved frozen copy, whatever its folder holds now;
        # a built-in one takes the settings the approval sealed (none for a policy).
        frozen = record["adapter"].get("sha256")
        adapter = declaration.read_folder(self.adapters.existing(frozen)) if frozen else None
        settings = {} if frozen else {"executor": None, "evaluator": None} | kind.sealed(record)
        written = {"format": templates.FORMAT, "name": fields.get("name"), "description": fields.get("description"), "version": 1, "artifactType": kind.type,
                   "adapter": {"folder": templates.ADAPTER_DIR} if frozen else {name: record["adapter"][name] for name in ("name", "version")}, **settings,
                   "cases": fields.get("cases"), "presentation": fields.get("presentation"),
                   "origin": {"datasetId": record["id"], "draftId": record["draftId"], "project": project_root(self.settings).name}}
        checked = templates.check(templates.contents(written, adapter), self._credentials())  # the same check as a folder edited by hand
        templates.require_valid(checked, "La plantilla", "No se guardó nada.")
        assert checked.declaration is not None
        if fields.get("path") is not None:
            target = request_path(fields["path"], base_dir, "path", "invalid-template").resolve()
        else:
            target = resolved_folders(self.settings)["templates"] / templates.folder_name(checked.declaration["name"])
        templates.write(target, templates.contents(checked.declaration, adapter))
        return self.template_check(target)

    def template_check(self, path: Path | str) -> dict[str, Any]:
        """Read a template folder and check it without the original and without loading its adapter's program: its view, with what the engine deduces,
        its hash and how to apply it, or its problems field by field. The full check, on the original, happens when it is applied. No model is called."""
        folder = Path(path).expanduser().resolve()
        return templates.view(folder, templates.check(templates.read(folder), self._credentials()))

    def template_list(self, *, artifact_type: str | None = None) -> dict[str, Any]:
        """The templates of the designated templates folder, by folder name; ``artifact_type`` keeps the valid ones for one type of artifact.

        Copies to share are left out: they are never applied.
        """
        if artifact_type is not None:
            _kind(artifact_type)
        folder = resolved_folders(self.settings)["templates"]
        credentials = self._credentials()
        found = []
        for entry in sorted(folder.iterdir()) if folder.is_dir() else []:
            if entry.name.startswith(".") or templates.is_share_copy(entry) or not (entry / templates.DECLARATION).is_file():
                continue
            checked = templates.check(templates.read(entry), credentials)
            if artifact_type is None or (checked.declaration is not None and checked.declaration["artifactType"] == artifact_type):
                found.append(templates.summary(entry, checked))
        return {"folder": str(folder), "templates": found}

    def template_init(self, example: str, *, path: Path | str | None = None) -> dict[str, Any]:
        """Copy one of GEPA's example templates, as it is, into a new folder: ``path``, or by default one named after the example in the
        designated templates folder. A folder that exists is never overwritten. Its view is the one ``check`` gives, with the example's name."""
        contents = templates.example(example)
        target = Path(path).expanduser().resolve() if path is not None else resolved_folders(self.settings)["templates"] / example
        templates.write(target, contents)
        return {**self.template_check(target), "example": example}

    def template_trust(self, path: Path | str, *, sha256: str | None = None) -> dict[str, Any]:
        """Record, with its date, that the person trusts the program of a template's adapter, identified by its SHA-256.

        Only the CLI offers it, for the person to run in their own terminal. ``sha256`` pins the program the agent explained: another content
        is refused with ``program-changed``, and nothing is recorded. Trusting the same program again keeps the date it was first trusted.
        """
        folder = Path(path).expanduser().resolve()
        contents = templates.read(folder)
        problem = next((item for item in contents.problems if item["field"] == templates.ADAPTER_DIR), None)
        if problem is not None:
            raise JobError(problem["message"], problem["code"])
        if contents.adapter is None:
            raise JobError(f"La plantilla {folder} no trae un programa propio ({templates.ADAPTER_DIR}/): se aplica sin confiar en nada.", "invalid-request")
        found = contents.adapter.sha256
        if sha256 is not None and sha256 != found:
            raise JobError(f"El programa de {folder} cambió desde que se pidió confiar en él (se pidió {sha256[:12]}; ahora es {found[:12]}): no se registró "
                           "nada. Vuelve a aplicar la plantilla para revisar el programa nuevo antes de confiar en él.", "program-changed")
        record = {"sha256": found, "trustedAt": _now(), "path": str(folder), "files": contents.adapter.digests()}
        if not self.store.insert("trust", found, record):
            record = self.store.get("trust", found)
        return {**record, "path": str(folder),
                "next": f"Aplica de nuevo la plantilla con \"template\": {{\"path\": \"{folder.as_posix()}\"}}: su programa ya no pide confianza en este proyecto."}

    # Internals ----------------------------------------------------------------------------

    def _credentials(self) -> Sequence[str]:
        """The credentials of the configured connections, from their environment variable or the encrypted store, long enough to find in a text."""
        lookup = credential_lookup(self.settings, open_secret_store(self.settings), self.environ)
        return [value for value in (lookup(connection.id) for connection in self.settings.connections) if value and len(value) >= custom.MIN_SECRET]

    def _applied_template(self, kind: Kind, document: Any, objective: str, request: Mapping[str, Any], base_dir: Path | str | None) -> tuple[dict[str, Any], Adapter]:
        """The template a prepare request names by its folder, applied to the original ``document``: what the draft records of it, and its adapter.

        The folder is read once. A copy to share is refused whatever it holds, since its holes may not even make it a template; otherwise the folder
        is checked, the program of an agent's adapter must be trusted, its settings are checked on this original, and a copy of it is
        frozen, so a change of the folder afterwards never reaches the draft, its approval or its jobs.
        """
        raw = request["template"]
        if not isinstance(raw, dict) or set(raw) != {"path"}:
            earlier = isinstance(raw, dict) and "id" in raw
            raise JobError(("Las plantillas ya no se nombran por un id 'tpl-…': ahora son carpetas del proyecto. " if earlier else "")
                           + "'template' debe ser {\"path\": \"<carpeta de la plantilla>\"}; búscalas con 'gepa template list'.", "invalid-template")
        folder = request_path(raw["path"], base_dir, "template.path", "invalid-template").resolve()
        if templates.is_share_copy(folder) and folder.is_dir():  # before reading it: even its declaration may be a hole
            raise JobError(templates.share_copy_next(folder), "template-is-share-copy")
        contents = templates.read(folder)
        checked = templates.check(contents, self._credentials())
        templates.require_valid(checked, f"La plantilla {folder}", f"Compruébala con: gepa template check {folder.as_posix()}")
        assert checked.declaration is not None
        declared = checked.declaration
        if declared["artifactType"] != kind.type:
            raise JobError(f"La plantilla {folder} es para artefactos de tipo {declared['artifactType']!r}, no {kind.type!r}: elige una plantilla para este artefacto.",
                           "template-artifact-mismatch")
        given = [name for name in ("executor", "evaluator", "adapter") if request.get(name) is not None]
        if given:
            raise JobError(f"Con una plantilla, {', '.join(repr(name) for name in given)} viene de la plantilla: quítalo de la solicitud, o edita los archivos "
                           f"de la plantilla para cambiarlo y compruébala con: gepa template check {folder.as_posix()}", "template-conflict")
        if contents.adapter is not None:  # an agent's adapter runs from a frozen copy of the template's own, as when a request names an adapter's folder
            self._require_trusted(folder, contents.adapter)  # before freezing it: its frozen copy is what exempts it afterwards
            adapter: Adapter = custom.CustomAdapter.load(self.adapters.freeze(contents.adapter), kind, document, objective=objective, runtime=self.runtime)
        else:
            adapter = kind.adapter(document, objective, executor=declared["executor"], evaluator=declared["evaluator"], workspaces=self.workspaces)
        self.template_copies.freeze(contents)
        return templates.application(folder, checked), adapter

    def _require_trusted(self, folder: Path, program: declaration.Folder) -> None:
        """Refuse, before loading it, the program of a template's adapter that this project never ran and the person never trusted.

        A program ran here when this project holds an intact frozen copy of it: a template saved here, or one applied here before. The error
        carries the command the person runs in their own terminal to trust it; the engine cannot tell who runs a command, so the skill tells
        the agent never to run it.
        """
        sha256 = program.sha256
        if self.store.get("trust", sha256) is not None:
            return
        try:
            if declaration.read_folder(self.adapters.path(sha256)).sha256 == sha256:
                return
        except declaration.AdapterError:
            pass
        trust = templates.trust_command(sys.executable, self.settings.home, folder, sha256)
        files = [f"{templates.ADAPTER_DIR}/{path}" for path, _ in program.files]
        raise JobError(f"La plantilla {folder} trae un programa propio ({templates.ADAPTER_DIR}/, sha256 {sha256[:12]}) que este proyecto nunca ejecutó y en el que "
                       "la persona no ha confiado: no se cargó. Explica a la persona qué hace según sus archivos, sin ejecutarlo, y dale este comando para que lo "
                       f"ejecute ella misma en su terminal; nunca lo ejecutes tú: {trust['command']}", "template-untrusted",
                       details={"trust": {**trust, "sha256": sha256, "files": files}})

    def _requested_adapter(self, kind: Kind, document: Any, objective: str, request: Mapping[str, Any], base_dir: Path | str | None) -> tuple[Adapter, str | None]:
        """The adapter a prepare request asks for, and its folder: the artifact's built-in adapter, or one an agent prepared, frozen now."""
        if request.get("adapter") is None:
            return kind.adapter(document, objective, executor=request.get("executor"), evaluator=request.get("evaluator"), workspaces=self.workspaces), None
        for key in ("executor", "evaluator"):
            if request.get(key) is not None:
                raise JobError(f"'{key}' no se indica con un adaptador propio: su adapter.json declara el ejecutor y el evaluador.", f"invalid-{key}")
        folder = declaration.request_folder(request["adapter"], base_dir)
        frozen = self.adapters.freeze(declaration.read_folder(folder))
        return custom.CustomAdapter.load(frozen, kind, document, objective=objective, runtime=self.runtime), str(folder)

    def _sealed_adapter(self, kind: Kind, document: Any, objective: str, record: Mapping[str, Any]) -> Adapter:
        """The adapter a draft, an approval or a manifest sealed, on ``document``: the built-in one with its settings, or an agent's frozen copy.

        An agent's adapter whose source folder changed since a draft or an approval is refused (a manifest records no source: a job runs its copy).
        """
        sealed = record["adapter"]
        if "sha256" in sealed:  # only an adapter an agent prepared is identified by the hash of its folder
            declaration.require_unchanged(record.get("adapterSource"), sealed["sha256"])
            return custom.CustomAdapter.load(self.adapters.existing(sealed["sha256"]), kind, document, objective=objective, runtime=self.runtime)
        return kind.adapter(document, objective, **kind.sealed(record), workspaces=self.workspaces)

    def _stored_adapter(self, record: Mapping[str, Any]) -> Adapter:
        """The adapter a draft, a sealed dataset or a job manifest was prepared with, rebuilt from what it sealed."""
        return self._sealed_adapter(_kind(record["artifact"]["type"]), record["artifact"]["original"], record["objective"], record)

    def _draft(self, draft_id: Any) -> dict[str, Any]:
        record = self.store.get("draft", draft_id) if isinstance(draft_id, str) else None
        if record is None:
            raise JobError(f"No existe el borrador '{draft_id}'; prepáralo con 'gepa dataset prepare'.", "draft-not-found")
        return cast(dict[str, Any], record)

    def _job(self, job_id: str) -> dict[str, Any]:
        job = self.store.get("job", job_id)
        if job is None:
            raise JobError(f"No existe el trabajo '{job_id}'.", "job-not-found")
        return self._refreshed(cast(dict[str, Any], job))

    def _lock_path(self, job_id: str) -> Path:
        return self.job_files / f"{job_id}.lock"

    def _search_folder(self, job_id: str) -> Path:
        """Where the job's search keeps GEPA's state: inside the data folder, never a folder a request names, because loading it runs a pickle."""
        return self.job_files / job_id / SEARCH_FILES

    def _refreshed(self, job: dict[str, Any]) -> dict[str, Any]:
        """The job as stored, or marked ``interrupted`` when it is ``running`` but no process holds its lock: its runner died."""
        path = self._lock_path(job["id"])
        if job["status"] != "running" or lifecycle.runner_alive(path):
            return job
        spent = lifecycle.search_spend(self.store, job["id"])  # its runner is dead: this record no longer changes

        def mark(current: dict[str, Any]) -> dict[str, Any]:
            if current["status"] == "running" and not lifecycle.runner_alive(path):  # checked again inside the transaction
                lifecycle.interrupt(current, search_spent=spent)
            return current

        return cast(dict[str, Any], self.store.update("job", job["id"], mark))

    def _launch(self, job_id: str) -> None:
        """Hand a queued attempt to the launcher; if it cannot start, the attempt is marked failed instead of left queued."""
        assert self.launcher is not None
        try:
            self.launcher(job_id)
        except Exception:
            def mark_failed(job: dict[str, Any]) -> dict[str, Any]:
                lifecycle.finish(job, "failed", error={"code": "launch-failed", "message": "No se pudo iniciar el proceso del trabajo."})
                _event(job, "failed", job["error"]["message"])
                return job
            self.store.update("job", job_id, mark_failed)
            raise JobError("No se pudo iniciar el proceso del trabajo en segundo plano.", "launch-failed") from None

    def _runnable(self, manifest: Mapping[str, Any]) -> tuple[Adapter, Sequence[dict[str, Any]]]:
        """The adapter and cases a job runs with, once its sealed dataset, frozen code and the adapter's requirements still hold. No model is called."""
        record = self._sealed(manifest["dataset"]["id"])
        if datasets.seal_sha256(record) != manifest["dataset"]["sha256"] or datasets.sha256_of(record["cases"]) != manifest["dataset"]["casesSha256"]:
            raise JobError("El dataset sellado no coincide con el manifiesto.", "dataset-changed")
        adapter = self._stored_adapter(manifest)  # an agent's adapter runs from its frozen copy, never from its source folder
        _require_frozen_code(manifest, adapter)
        adapter.verify_requirements()
        return adapter, record["cases"]

    def _artifact(self, raw: Any, base_dir: Path | str | None) -> tuple[Kind, Any, str | None]:
        """The artifact's kind, its original document and the file or folder it was read from (``None`` when given inline)."""
        if not isinstance(raw, dict) or raw.get("type") not in KINDS:
            raise JobError(FORMS, "invalid-artifact")
        kind = KINDS[raw["type"]]
        document, source = kind.load(raw, base_dir)
        return kind, document, source

    def _models(self, raw: Any, roles: Mapping[str, str]) -> dict[str, Any]:
        raw = {} if raw is None else raw
        if not isinstance(raw, dict) or set(raw) - set(roles) or not all(isinstance(value, str) for value in raw.values()):
            raise JobError(f"'models' admite {' y '.join(repr(role) for role in roles)} con el id de una conexión.", "invalid-models")
        secrets = open_secret_store(self.settings)
        frozen = {}
        for role, settings_role in roles.items():
            identifier = raw.get(role) or self.settings.roles.get(settings_role)
            if not identifier:
                raise JobError(f"Falta el modelo '{role}': indícalo en models.{role} o asigna el rol con 'gepa setup role {settings_role} <conexión>'.", "role-missing")
            connection = self.settings.connection(identifier)
            if connection is None:
                raise JobError(f"No existe la conexión '{identifier}'; revisa 'gepa setup show'.", "connection-missing")
            if (connection.provider == "openrouter" or connection.api_key_env) and not has_credential(connection, secrets, self.environ):
                raise JobError(f"La conexión '{identifier}' no tiene credencial; configúrala antes de gastar presupuesto.", "credential-missing")
            frozen[role] = {"connection": connection.id, "name": connection.name, "provider": connection.provider, "url": connection.url,
                            "model": connection.model, "apiKeyEnv": connection.api_key_env, "settingsRole": settings_role, "sampling": SAMPLING_NOTE}
        return frozen

    def _limits(self, raw: Any, counts: Mapping[str, int], adapter: Adapter) -> dict[str, Any]:
        raw = {} if raw is None else raw
        allowed = LIMIT_KEYS | set(adapter.limit_defaults)
        if not isinstance(raw, dict) or set(raw) - allowed:
            raise JobError(f"'limits' admite: {', '.join(sorted(allowed))}.", "invalid-limits")
        minibatch = min(_integer(raw, "reflectionMinibatchSize", 3, 1, 100), counts["train"])
        proposals = _integer(raw, "maxProposals", 5, 1, 1000)
        iteration_cost = _iteration_cost(minibatch, counts["val"])
        minimum = counts["val"] + iteration_cost
        metric_calls = _integer(raw, "maxMetricCalls", counts["val"] + proposals * iteration_cost, 1, 10_000_000)
        if metric_calls < minimum:
            raise JobError(f"maxMetricCalls={metric_calls} no alcanza para validar el original y completar una iteración: mínimo {minimum}.", "budget-too-small")
        minutes = raw.get("timeLimitMinutes", 60)
        if not _finite(minutes) or not 0 < minutes <= 1440:
            raise JobError("'timeLimitMinutes' debe estar entre 0 y 1440.", "invalid-limits")
        tokens = {key: _integer(raw, key, default, 1, MAX_OUTPUT_TOKENS) for key, default in adapter.limit_defaults.items()}
        return {"maxMetricCalls": metric_calls, "maxProposals": proposals, "reflectionMinibatchSize": minibatch, "timeLimitMinutes": minutes,
                **tokens, "maxFinalEvaluations": (MAX_FINALIST_PROPOSALS + 1) * counts["test"]}

    def _requester(self, manifest: Mapping[str, Any]) -> Callable[..., dict[str, Any]]:
        """Model calls always use the connections frozen in the manifest, never today's settings."""
        frozen = {role: make_connection(identifier=m["connection"], provider=m["provider"], model=m["model"], url=m["url"], name=m["name"], api_key_env=m["apiKeyEnv"])
                  for role, m in manifest["models"].items()}
        connections = {role: connection.as_provider_dict() for role, connection in frozen.items()}
        gateway = self.gateway
        if gateway is None:
            snapshot = replace(self.settings, connections=tuple({connection.id: connection for connection in frozen.values()}.values()))
            gateway = ModelGateway(api_keys=credential_lookup(snapshot, open_secret_store(self.settings), self.environ))

        def request(role: str, messages: list[dict[str, str]], *, max_tokens: int, timeout: float) -> dict[str, Any]:
            return gateway.complete(connections[role], messages, max_tokens=max_tokens, timeout=timeout)

        return request

    def _view(self, job: Mapping[str, Any], *, cases: bool = False) -> dict[str, Any]:
        """The one structured job view; the CLI (--json) and the MCP tools return exactly this."""
        result = job.get("result") or {}
        selection = result.get("selection") or {}
        artifact_key = _view_key(job["manifest"])
        candidates = []
        for candidate in result.get("candidates", []):
            item = {"id": candidate["id"], "label": candidate["label"], "origin": candidate["origin"], "parents": candidate["parents"],
                    artifact_key: stored_artifact(candidate), "validation": _summary(candidate["validation"]), "test": _summary(candidate["test"]),
                    "finalist": candidate["id"] in selection.get("finalistIds", []), "selected": candidate["id"] == selection.get("selectedId")}
            if cases:
                item["cases"] = {"search": candidate["searchCases"], "validation": candidate["validation"]["cases"],
                                 "test": (candidate["test"] or {}).get("cases", []), "earlier": candidate.get("earlierCases", [])}
            candidates.append(item)
        attempts = job.get("attempts", [])
        cancel = lifecycle.cancel_requested(self.store, job["id"], attempts[-1]["attempt"]) if job["status"] == "running" and attempts else None
        recovery = lifecycle.recovery(job)
        return {
            "jobId": job["id"], "name": job["name"], "status": job["status"], "phase": job["phase"],
            "createdAt": job["createdAt"], "startedAt": job["startedAt"], "updatedAt": job["updatedAt"], "finishedAt": job["finishedAt"],
            "manifestSha256": job["manifestSha256"], "manifest": job["manifest"], "error": job["error"],
            "baselineId": result.get("baselineId"), "selection": result.get("selection"), "finalCheck": result.get("finalCheck"),
            "counts": result.get("counts"), "usage": result.get("usage"), "costUsd": result.get("costUsd"),
            "consumption": lifecycle.consumption(job), "attempts": attempts, "cancelRequested": cancel, "recovery": recovery,
            "searchStopReason": result.get("searchStopReason"), "stopReason": result.get("stopReason"), "searchState": result.get("searchState"),
            "candidates": candidates, "proposals": result.get("proposals", []), "events": job["events"],
            "next": _job_next(job, cancel, recovery),
        }


def _view_key(manifest: Mapping[str, Any]) -> str:
    """What views call a candidate's artifact: ``policy`` for a JEV policy, ``skill`` for a Skill."""
    return _kind(manifest["artifact"]["type"]).view_key


def _job_next(job: Mapping[str, Any], cancel: Mapping[str, Any] | None, recovery: Mapping[str, Any] | None) -> str:
    """What a host agent can do next with a job, from its state alone."""
    job_id = job["id"]
    if job["status"] in lifecycle.ACTIVE:
        if cancel:
            return f"Cancelación solicitada ({cancel['at']}): el trabajo se detiene antes de su siguiente llamada. Consulta con: gepa job show {job_id}"
        if job["status"] == "queued":
            return (f"En cola hasta que su proceso lo tome. Consulta con: gepa job show {job_id}. Si sigue en cola pasado un minuto, revisa "
                    f"<datos>/{JOB_FILES}/{job_id}.log; puedes ejecutarlo con: gepa job run {job_id}, o cancelarlo con: gepa job cancel {job_id}")
        return f"Consulta el progreso con: gepa job show {job_id} · cancélalo con: gepa job cancel {job_id}"
    review = f"Revisa la evidencia y la recomendación con: gepa job review {job_id}"
    if recovery is None:
        return review
    if recovery["retryable"] and recovery["kind"] is None:  # the person chooses
        names = {"continue-search": "Continuar la búsqueda", "final-retry": "Cerrar con lo validado", "run": "Ejecutar completo"}
        return f"{recovery['message']} {' · '.join(names[option['kind']] + ': ' + option['cli'] for option in recovery['options'])} · {review}"
    if recovery["retryable"]:
        return f"{recovery['message']} Reintenta con: {recovery['cli']} · {review}"
    return f"{recovery['message']} {review}"


def _template_request(request: Any) -> dict[str, Any]:
    """A request to save a template: a JSON object with known fields. What it would write is checked as a template folder is, credentials included."""
    if not isinstance(request, dict):
        raise JobError("La solicitud para guardar una plantilla debe ser un objeto JSON.")
    unknown = set(request) - templates.SAVE_KEYS
    if unknown:
        raise JobError(f"Campos no admitidos para guardar una plantilla: {', '.join(sorted(unknown))}.", "invalid-template")
    return request


def _require_current_adapter(record: Mapping[str, Any], adapter: Adapter) -> None:
    """A draft or an approval holds only for the adapter and evaluator code that prepared and previewed it."""
    described = adapter.describe()
    if record["adapterCodeSha256"] != _code_fingerprint(adapter.modules) or any(record[key] != described[key] for key in described):
        raise JobError("El adaptador o el evaluador instalados cambiaron desde que se preparó este dataset; prepáralo, previsualízalo y apruébalo de nuevo.", "approval-outdated")


def _require_frozen_code(manifest: Mapping[str, Any], adapter: Adapter) -> None:
    """A job runs only with the adapter, evaluator, GEPA version and engine code its manifest froze."""
    described = adapter.describe()
    changed = [key for key in ("adapter", "evaluator", "mutableSurface", "fixedContract") if manifest[key] != described[key]]
    if manifest["engine"]["gepa"] != _engine_version():
        changed.append("gepa")
    if manifest["engine"]["capability"] != __version__:
        changed.append("capability")
    if manifest["engine"]["codeSha256"] != _code_fingerprint():
        changed.append("code")
    if changed:
        raise JobError(f"El código instalado no coincide con el manifiesto congelado ({', '.join(changed)}); crea un trabajo nuevo.", "engine-changed")


def _saved_search(job: Mapping[str, Any], folder: Path, data_dir: Path) -> dict[str, Any]:
    """What the engine kept in the GEPA state a job's search saved last, once that state is this job's own: for its manifest and GEPA version,
    with the original first and only candidates the job validated completely. ``folder`` is the job's search folder, and the state is loaded
    only if it really lives in ``data_dir`` (no link out of it), because loading it runs the pickle it holds. A state that is missing, damaged
    or not this job's is refused; nothing is changed.
    """
    job_id = job["id"]
    after = (f"La evidencia del trabajo no cambió. Puedes cerrar con los candidatos ya validados (gepa job retry {job_id} --kind final-retry) "
             "o crear un trabajo nuevo con la misma especificación.")
    path = folder / GEPA_STATE
    if not path.is_file():
        raise JobError(f"No hay un estado guardado de GEPA para el trabajo {job_id} ({folder}): su búsqueda no se puede continuar. {after}", "search-state-missing")
    if not path.resolve().is_relative_to(data_dir.resolve()):
        raise JobError(f"El estado guardado de GEPA del trabajo {job_id} está fuera de la carpeta de datos del motor (un enlace en {folder}): no se carga, "
                       f"porque cargarlo ejecuta su contenido. {after}", "search-state-mismatch")
    from gepa.core.state import GEPAState

    try:
        state: GEPAState[Any, Any] = GEPAState.load(str(folder))
        state.is_consistent()  # raises when the saved candidates, scores and fronts do not match
    except Exception:
        raise JobError(f"El estado guardado de GEPA del trabajo {job_id} está dañado y no se puede cargar: su búsqueda no se puede continuar. {after}",
                       "search-state-damaged") from None
    saved = state.adapter_state
    result = job.get("result") or {}
    candidates = {candidate["id"]: candidate for candidate in result.get("candidates", [])}
    programs = [_candidate_id(program) for program in state.program_candidates]
    ours = (saved.get("format") == SEARCH_STATE_FORMAT and saved.get("jobId") == job_id and saved.get("manifestSha256") == job["manifestSha256"]
            and saved.get("gepa") == job["manifest"]["engine"]["gepa"])
    counted = all(isinstance(saved.get(key), int) and 0 <= saved[key] <= len(result.get(key, [])) for key in ("candidates", "proposals"))
    validated = all((candidates.get(identifier, {}).get("validation") or {}).get("status") == "complete" for identifier in programs)
    if not (ours and counted and validated and programs[:1] == [result.get("baselineId")]):
        raise JobError(f"El estado guardado de GEPA en {folder} no corresponde a la búsqueda del trabajo {job_id} (otro trabajo, otro manifiesto, otra "
                       f"versión de GEPA o candidatos que el trabajo no validó): no se continúa desde él. {after}", "search-state-mismatch")
    return dict(saved)


class _Run:
    """GEPA adapter for one attempt of a job: evaluates candidates, records evidence and what each phase spent, and enforces the frozen limits.

    A ``run`` attempt searches and then evaluates the finalists on the reserved test; a ``final-retry`` attempt continues a stored
    result whose selection is frozen and evaluates only the reserved test cases still pending; a ``continue-search`` attempt continues
    a search cut short from the state GEPA saved in ``folder`` (``saved`` is what the engine kept in it), then runs the reserved test.
    Before each model call and each case the attempt stops at a safe point if someone asked to cancel it or its time ran out.
    """

    def __init__(self, job: dict[str, Any], adapter: Adapter, cases: Sequence[dict[str, Any]], request: Callable[..., dict[str, Any]], persist: Callable[[], None],
                 *, cancelled: Callable[[], Mapping[str, Any] | None], record_spend: Callable[[int], None], folder: Path, saved: Mapping[str, Any] | None = None) -> None:
        self.job = job
        self.manifest = job["manifest"]
        self.limits = self.manifest["limits"]
        self.adapter = adapter
        self.request_model = request
        self.persist = persist
        self.cancelled = cancelled
        self.record_spend = record_spend  # records the search evaluations started, before each one
        self.folder = folder
        self.saved = saved
        self.attempt = lifecycle.attempt_of(job)
        self.rows = {split: [case for case in cases if case["split"] == split] for split in datasets.SPLITS}
        self.val_ids = {case["id"] for case in self.rows["val"]}
        # Import GEPA before the clock starts: a cold import is not part of the job's time budget.
        import gepa
        from gepa.core.adapter import EvaluationBatch

        self.optimize, self.evaluation_batch = gepa.optimize, EvaluationBatch
        self.deadline = time.monotonic() + self.limits["timeLimitMinutes"] * 60  # each attempt has its own time limit
        # GEPA swallows (and retries) exceptions raised while proposing; defer them to its next stop check.
        self.pending: Exception | None = None
        self.search_stop: str | None = None
        self.iterations = 0  # iterations in GEPA's state, the unit of maxProposals: an iteration a continuation repeats counts once
        # A continuation first replays the original's validation, which GEPA asks for before loading its state; the candidates proposed
        # after that state was saved (lost with the cut) may be proposed and evaluated again, their earlier results kept in earlierCases.
        self.replaying = saved is not None
        self.lost: set[str] = set()
        self.last_saved = -math.inf
        self.phase: str | None = None
        self.phase_started = 0.0
        self.cancel_request: Mapping[str, Any] | None = None
        self.texts: dict[str, dict[str, str]] = {}
        self.by_id: dict[str, dict[str, Any]] = {}
        if self.attempt["kind"] in ("final-retry", "continue-search"):
            self.result: dict[str, Any] = job["result"]
            for record in self.result["candidates"]:
                self.by_id[record["id"]] = record
                self.texts[record["id"]] = record["texts"]  # the exact texts GEPA evaluated, not a re-encoding of the stored artifact
            calls = sum(self.result["counts"]["modelCalls"].values())
            self.cost = self.result["costUsd"] or 0.0
            self.cost_known = self.result["costUsd"] is not None or not calls  # an unknown cost stays unknown
            return
        self.cost = 0.0
        self.cost_known = True
        self.result = {
            "baselineId": None, "candidates": [], "proposals": [], "selection": None, "finalCheck": None,
            "counts": {"searchEvaluations": 0, "finalEvaluations": 0, "iterations": 0, "modelCalls": dict.fromkeys(adapter.roles, 0), "internalChecks": 0},
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, "costUsd": None,
            "searchStopReason": None, "stopReason": None,
        }
        job["result"] = self.result
        self.result["baselineId"] = self.register(adapter.seed(), origin="original", parent=None)["id"]

    # Bookkeeping ------------------------------------------------------------------------

    def register(self, texts: Mapping[str, str], *, origin: str, parent: str | None) -> dict[str, Any]:
        identifier = _candidate_id(texts)
        record = self.by_id.get(identifier)
        if record is None:
            number = len(self.by_id)
            record = {"id": identifier, "label": "Original" if number == 0 else f"Candidato {number}", "origin": origin, "parents": [],
                      "artifact": self.adapter.artifact(texts), "texts": dict(texts), "searchCases": [], "validation": _measurement(len(self.val_ids)), "test": None}
            self.by_id[identifier] = record
            self.texts[identifier] = dict(texts)
            self.result["candidates"].append(record)
        if parent and parent != identifier and parent not in record["parents"]:
            record["parents"].append(parent)
        return record

    def begin(self, phase: str, max_evaluations: int) -> None:
        """Enter a phase: the job shows it, and this attempt accounts its evaluations, calls, tokens, cost and seconds apart."""
        self.phase = phase
        self.job["phase"] = self.attempt["phase"] = phase
        self.attempt["phases"][phase] = lifecycle.new_meter(max_evaluations, self.adapter.roles)
        self.phase_started = time.monotonic()

    @property
    def meter(self) -> dict[str, Any]:
        assert self.phase is not None
        return cast(dict[str, Any], self.attempt["phases"][self.phase])

    def record_seconds(self) -> None:
        """Record the seconds spent in the current phase: stored with the job, never computed when it is read."""
        if self.phase is not None:
            self.meter["seconds"] = round(time.monotonic() - self.phase_started, 3)

    def save(self, *, throttle: bool = False) -> None:
        """Persist the job. Per-case progress passes ``throttle`` so a long run is not rewritten after every case."""
        calls = sum(self.result["counts"]["modelCalls"].values())
        self.result["costUsd"] = self.cost if self.cost_known and calls else None
        now = time.monotonic()
        if throttle and now - self.last_saved < PROGRESS_SAVE_SECONDS:
            return
        self.last_saved = now
        self.record_seconds()
        self.persist()

    def check(self, *, metric: bool = False) -> None:
        """A safe point, before a model call or a case: stop if someone asked to cancel, the time ran out or the search budget is spent."""
        request = self.cancelled()
        if request is not None:
            self.cancel_request = request
            raise Stop("cancelled")
        self.check_time()
        if metric and self.result["counts"]["searchEvaluations"] >= self.limits["maxMetricCalls"]:
            raise Stop("budget")

    def requested(self) -> str:
        """When the cancellation this attempt honoured was requested, for its event."""
        return f"solicitada {self.cancel_request['at']}" if self.cancel_request else "solicitada"

    def check_time(self) -> None:
        if time.monotonic() >= self.deadline:
            raise Stop("time_limit")

    def call(self, role: str, messages: list[dict[str, str]], max_tokens: int | None = None) -> dict[str, Any]:
        """One model call under the frozen limits; an adapter whose output limit is sealed with its approval passes ``max_tokens``."""
        self.check()
        meter = self.meter
        self.result["counts"]["modelCalls"][role] += 1
        meter["modelCalls"][role] += 1
        max_tokens = max_tokens or self.limits[f"{role}MaxTokens"]
        timeout = min(600.0, max(0.001, self.deadline - time.monotonic()))
        try:
            answer = self.request_model(role, messages, max_tokens=max_tokens, timeout=timeout)
        except BaseException as error:
            # A failed or interrupted call may have been billed: from here on the total cost is unknown.
            self.cost_known = False
            self.result["costUsd"] = None
            self.result["usage"] = dict.fromkeys(self.result["usage"])
            meter.update(usage=dict.fromkeys(meter["usage"]), costUsd=None)
            if isinstance(error, Exception):
                self.check_time()  # a provider timeout at the deadline is a time-limit stop, not a failure
            raise
        self.result["usage"] = _added_usage(self.result["usage"], answer.get("usage"))
        meter["usage"] = _added_usage(meter["usage"], answer.get("usage"))
        cost = answer.get("costUsd")
        if _known_cost(cost):
            self.cost += cost
            meter["costUsd"] = None if meter["costUsd"] is None else meter["costUsd"] + cost
        else:
            self.cost_known = False
            meter["costUsd"] = None
        return answer

    def evaluate_case(self, texts: Mapping[str, str], case: Mapping[str, Any], phase: str) -> dict[str, Any]:
        counts = self.result["counts"]
        if phase == "search":
            self.check(metric=True)
            counts["searchEvaluations"] += 1
            self.record_spend(counts["searchEvaluations"])  # before the case: if the runner dies in it, the budget still counts it
        else:
            self.check()
            if self.meter["evaluations"] >= self.meter["maxEvaluations"]:
                raise Stop("budget")  # each attempt evaluates each pending case at most once
            counts["finalEvaluations"] += 1
            self.result["finalCheck"]["evaluations"] += 1
        self.meter["evaluations"] += 1
        result = {**self.adapter.run_case(texts, case, _Requests(self)), "phase": phase, "attempt": self.attempt["attempt"]}
        # Checks inside the case, counted only once it ran; a judge's scores are submetrics, not checks.
        counts["internalChecks"] += sum(isinstance(value, bool) for value in result["submetrics"].values())
        return result

    # GEPA adapter protocol --------------------------------------------------------------

    def evaluate(self, batch: list[dict[str, Any]], candidate: dict[str, str], capture_traces: bool = False) -> Any:
        if self.pending is not None:
            raise self.pending
        full_validation = len(batch) == len(self.val_ids) and {case["id"] for case in batch} == self.val_ids
        if self.replaying:
            return self.replay(candidate, batch, full_validation)
        record = self.register(candidate, origin="reflection", parent=None)
        if full_validation:
            self.set_aside(record, record["validation"]["cases"])
            record["validation"] = {**_measurement(len(self.val_ids)), "status": "partial"}
        outputs = []
        for case in batch:
            result = self.evaluate_case(candidate, case, "search")
            outputs.append(result)
            if case["split"] == "val":
                bucket = record["validation"]["cases"]
                if record["validation"]["status"] == "pending":
                    record["validation"]["status"] = "partial"
            else:
                bucket = record["searchCases"]
            self.set_aside(record, [old for old in bucket if old["caseId"] == result["caseId"]])
            bucket[:] = [old for old in bucket if old["caseId"] != result["caseId"]] + [result]
            self.save(throttle=True)
        if full_validation:
            validation = record["validation"]
            _complete(validation, outputs)
            _event(self.job, "validation", f"{record['label']} ({record['id']}): validación completa, {validation['correct']:g} de {validation['total']}.")
            self.save()
        return self.evaluation_batch(outputs=outputs, scores=[result["score"] for result in outputs], trajectories=outputs if capture_traces else None)

    def replay(self, candidate: Mapping[str, str], batch: list[dict[str, Any]], full_validation: bool) -> Any:
        """The original's validation from the stored evidence, without a model call: GEPA asks for it before loading the state it continues from."""
        self.replaying = False
        original = self.by_id[self.result["baselineId"]]
        if _candidate_id(candidate) != original["id"] or not full_validation or original["validation"]["status"] != "complete":
            raise JobError("Al continuar, GEPA pidió una evaluación que no es la validación completa del original; no se continúa.", "search-state-mismatch")
        stored = {result["caseId"]: result for result in original["validation"]["cases"]}
        outputs = [stored[case["id"]] for case in batch]
        return self.evaluation_batch(outputs=outputs, scores=[result["score"] for result in outputs], trajectories=None)

    def set_aside(self, record: dict[str, Any], results: list[dict[str, Any]]) -> None:
        """Keep in ``earlierCases`` the results an earlier attempt stored that this attempt is about to replace: a continuation never erases them."""
        earlier = [result for result in results if result["attempt"] != self.attempt["attempt"]]
        if earlier:
            record.setdefault("earlierCases", []).extend(earlier)

    def make_reflective_dataset(self, candidate: dict[str, str], eval_batch: Any, components_to_update: list[str]) -> dict[str, list[dict[str, Any]]]:
        # Reflection learns only from training feedback, even if GEPA ever passed other cases.
        records = [self.adapter.reflective_record(result) for result in (eval_batch.trajectories or []) if result["split"] == "train"]
        return {component: records for component in components_to_update}

    def propose_new_texts(self, candidate: dict[str, str], reflective_dataset: Mapping[str, Any], components_to_update: list[str]) -> dict[str, str]:
        """Return the new texts, or ``{}`` so GEPA skips the child without spending evaluations.

        Returning the parent instead would make GEPA re-evaluate it as a child and, with a noisy
        decider, even accept it and overwrite its validation.
        """
        parent = self.register(candidate, origin="reflection", parent=None)
        if self.pending is not None:
            return {}
        self.result["counts"]["iterations"] += 1
        proposal: dict[str, Any] = {"iteration": self.result["counts"]["iterations"], "parentId": parent["id"], "status": None,
                                    "candidateId": None, "reason": None, "output": None}
        self.result["proposals"].append(proposal)
        try:
            answer = self.call("reflection", self.adapter.reflection_messages(candidate, list(reflective_dataset.get(self.adapter.component, []))))
        except Exception as error:
            # GEPA swallows exceptions raised here; ``pending`` stops the search and re-raises after it.
            self.pending = error
            proposal.update(status="failed", reason=error.reason if isinstance(error, Stop) else _public_error(error)["message"])
            self.save()
            return {}
        text = answer.get("text")
        proposal["output"] = storable(text[:MAX_PROPOSAL_KEPT]) if isinstance(text, str) else None  # kept even if the model wrote a lone surrogate
        try:
            texts = self.adapter.parse_proposal(text)
        except ContractError as error:
            proposal.update(status="rejected", reason=str(error))
            self.save()
            return {}
        identifier = _candidate_id(texts)
        if identifier in self.by_id and identifier not in self.lost:
            # Each candidate is validated once: a repeated artifact is not a new child.
            proposal.update(status="rejected", reason=f"La propuesta repite {_kind(self.adapter.artifact_type).noun} de {self.by_id[identifier]['label']} ({identifier}).")
            self.save()
            return {}
        self.lost.discard(identifier)  # a child lost with the cut is evaluated again as it would have been after a pause
        child = self.register(texts, origin="reflection", parent=parent["id"])
        proposal.update(status="valid", candidateId=child["id"])
        self.save()
        return texts

    def should_stop(self, gepa_state: Any) -> bool:
        """GEPA's check before each iteration. A failure deferred from a proposal is raised here, before GEPA saves that iteration as done, so
        a continuation repeats it. ``maxProposals`` counts the iterations in GEPA's state; ``maxMetricCalls``, every evaluation the job spent."""
        self.iterations = gepa_state.i + 1
        if self.pending is not None:
            raise self.pending
        if self.iterations >= self.limits["maxProposals"]:
            self.search_stop = "max_proposals"
            return True
        if self.result["counts"]["searchEvaluations"] + _iteration_cost(self.limits["reflectionMinibatchSize"], len(self.val_ids)) > self.limits["maxMetricCalls"]:
            self.search_stop = "budget"
            return True
        return False

    def get_adapter_state(self) -> dict[str, Any]:
        """What GEPA saves with its state at the start of each iteration: the job, manifest and GEPA version it belongs to, and how many
        candidates and proposals the job had then (a continuation tells by them what the cut left out of GEPA's state)."""
        return {"format": SEARCH_STATE_FORMAT, "jobId": self.job["id"], "manifestSha256": self.job["manifestSha256"], "gepa": self.manifest["engine"]["gepa"],
                "attempt": self.attempt["attempt"], "candidates": len(self.result["candidates"]), "proposals": len(self.result["proposals"])}

    def set_adapter_state(self, state: dict[str, Any]) -> None:
        """GEPA hands back what it saved with the state it loaded: nothing on a new search, and on a continuation the state checked before it began.
        Every candidate registered after that state was saved is unknown to GEPA, even one whose validation was stored just before the cut."""
        if state != dict(self.saved or {}):
            raise JobError("El estado de GEPA que se cargó no es el que se comprobó antes de empezar el intento; no se continúa desde él.", "search-state-mismatch")
        if self.saved is not None:
            self.lost = {candidate["id"] for candidate in self.result["candidates"][state["candidates"]:]}

    def on_state_saved(self, event: Mapping[str, Any]) -> None:
        """GEPA saved its state at the start of an iteration (``iteration`` is how many it completed): a continuation would start from here."""
        self.state_saved(event["iteration"])

    def state_saved(self, iterations: int) -> None:
        self.result["searchState"] = {"attempt": self.attempt["attempt"], "iterations": iterations, "savedAt": _now()}
        self.save()

    # Phases -----------------------------------------------------------------------------

    def execute(self) -> None:
        if self.attempt["kind"] == "final-retry":
            if self.result["selection"] is None:
                self.freeze_selection()
            self.final_check()
            return
        spent = self.result["counts"]["searchEvaluations"]
        self.begin("search", self.limits["maxMetricCalls"] - spent)  # the job's search budget, not reset by a continuation
        case_counts = f"{len(self.rows['train'])} casos de entrenamiento y {len(self.rows['val'])} de validación"
        if self.saved is None:
            message = f"Búsqueda GEPA con {case_counts}; {len(self.rows['test'])} casos de prueba reservada quedan fuera de la búsqueda."
        else:
            self.result.update(searchStopReason=None, stopReason=None)  # why the earlier attempt stopped stays in that attempt and its events
            state = self.result["searchState"]
            message = (f"Búsqueda GEPA continuada con {case_counts} desde el estado guardado al empezar la iteración {state['iterations'] + 1} ({state['savedAt']}): "
                       f"no se repiten las {state['iterations']} iteraciones completas y la validación del original se toma de la evidencia guardada. "
                       f"Lo que el intento anterior hizo después de ese estado ({len(self.result['proposals']) - self.saved['proposals']} propuestas) se "
                       f"conserva, y lo que se repita cuenta como gasto nuevo. Presupuesto del trabajo: van {state['iterations']} de "
                       f"{self.limits['maxProposals']} iteraciones y {spent} de {self.limits['maxMetricCalls']} evaluaciones. La prueba reservada "
                       "sigue fuera de la búsqueda.")
        _event(self.job, "search", message)
        self.save()
        try:
            self.check()
            self.folder.mkdir(parents=True, exist_ok=True)
            self.optimize(
                seed_candidate=self.adapter.seed(), trainset=self.rows["train"], valset=self.rows["val"], adapter=cast(Any, self),
                max_metric_calls=self.limits["maxMetricCalls"], reflection_minibatch_size=self.limits["reflectionMinibatchSize"],
                seed=self.manifest["seed"], logger=_SilentLogger(), display_progress_bar=False, raise_on_exception=True,
                skip_perfect_score=False, use_merge=False, cache_evaluation=False, stop_callbacks=[self.should_stop],
                run_dir=str(self.folder), callbacks=[cast(Any, self)],  # GEPA saves its state there and tells on_state_saved
            )
            self.state_saved(self.iterations)  # GEPA saves its state once more when its search ends
            if self.pending is not None:
                raise self.pending
            reason = self.search_stop or "completed"
        except Stop as stop:
            reason = stop.reason
        self.result["searchStopReason"] = reason
        if reason == "time_limit":
            self.stop("time_limit", "Se alcanzó el límite de tiempo durante la búsqueda; no se ejecutó la prueba reservada.")
            return
        if reason == "cancelled":
            self.stop("cancelled", f"Cancelado a petición ({self.requested()}) durante la búsqueda: no empezó ninguna llamada nueva, los candidatos y la "
                                   "evidencia guardados se conservan y no se ejecutó la prueba reservada.")
            return
        self.freeze_selection()
        self.final_check()

    def freeze_selection(self) -> None:
        baseline = self.by_id[self.result["baselineId"]]
        if baseline["validation"]["status"] != "complete":
            raise JobError("La validación del original no se completó; no hay base de comparación.", "baseline-incomplete")
        ranked = sorted((item for item in self.result["candidates"] if item is not baseline and item["validation"]["status"] == "complete"),
                        key=lambda item: -item["validation"]["score"])
        finalists = [baseline, *ranked[:MAX_FINALIST_PROPOSALS]]
        selected = max(finalists, key=lambda item: item["validation"]["score"])  # first maximum: ties keep the original
        self.result["selection"] = {
            "basis": "validation", "rule": f"Mayor {self.adapter.metric_label} en la validación completa; en caso de empate se conserva el original.",
            "selectedId": selected["id"], "selectedIsOriginal": selected is baseline, "finalistIds": [item["id"] for item in finalists], "frozenAt": _now(),
        }
        for item in finalists:
            item["test"] = _measurement(len(self.rows["test"]))
        stopped = self.result["searchStopReason"] not in lifecycle.NORMAL_SEARCH_ENDS
        where = (f" La búsqueda se detuvo antes de terminar (motivo {self.result['searchStopReason']}): se eligió entre los {len(ranked) + 1} candidatos con "
                 "validación completa, sin continuar GEPA." if stopped else "")
        _event(self.job, "selection", f"Selección congelada por validación antes de la prueba reservada: {selected['label']} ({selected['id']}); "
                                      f"{len(finalists)} finalistas.{where}")
        self.save()

    def resolved_tests(self) -> int:
        """Keep each finalist's valid reserved test results, dropping any other, and count them; a complete measurement stays as it is."""
        test_ids = [case["id"] for case in self.rows["test"]]
        resolved = 0
        for identifier in self.result["selection"]["finalistIds"]:
            measurement = self.by_id[identifier]["test"]
            if measurement["status"] != "complete":
                kept: dict[str, dict[str, Any]] = {}
                for record in measurement["cases"]:
                    if record["caseId"] in test_ids and _finite(record.get("score")):
                        kept.setdefault(record["caseId"], record)
                measurement["cases"] = list(kept.values())
            resolved += len(measurement["cases"])
        return resolved

    def final_check(self) -> None:
        """Evaluate the frozen finalists on the reserved test; a retry evaluates only the cases earlier attempts left pending."""
        finalist_ids = self.result["selection"]["finalistIds"]
        order = [case["id"] for case in self.rows["test"]]
        retry = self.result["finalCheck"] is not None
        required = len(finalist_ids) * len(order)
        final = self.result["finalCheck"] or {"status": "running", "split": "test", "total": len(order), "required": required, "resolved": 0,
                                              "evaluations": 0, "attempts": 0}
        final.update(status="running", resolved=self.resolved_tests(), attempts=final["attempts"] + 1)
        self.result["finalCheck"] = final
        pending = required - final["resolved"]
        self.begin("final", pending)  # this attempt's limit: one evaluation per pending case
        if retry:
            message = (f"Reintento de la prueba reservada: {pending} casos pendientes entre {len(finalist_ids)} versiones; "
                       "los resultados completos se reutilizan y GEPA no se repite.")
        elif self.attempt["kind"] == "final-retry":
            message = (f"Prueba reservada tras una búsqueda que se detuvo antes de terminar: {len(finalist_ids)} versiones sobre los mismos "
                       f"{len(order)} casos. Se cerró con lo validado, sin continuar GEPA.")
        else:
            message = f"Prueba reservada: {len(finalist_ids)} versiones sobre los mismos {len(order)} casos. GEPA ya terminó."
        _event(self.job, "final-check", message)
        self.save()
        try:
            for identifier in finalist_ids:
                record = self.by_id[identifier]
                measurement = record["test"]
                if measurement["status"] == "complete":
                    continue
                measurement["status"] = "running"
                done = {case["caseId"] for case in measurement["cases"]}
                for case in self.rows["test"]:
                    if case["id"] not in done:
                        measurement["cases"].append(self.evaluate_case(self.texts[identifier], case, "final"))
                        final["resolved"] += 1
                        self.save(throttle=True)
                measurement["cases"].sort(key=lambda item: order.index(item["caseId"]))
                _complete(measurement, measurement["cases"])
                _event(self.job, "final-result", f"{record['label']} ({identifier}): prueba reservada completa, {measurement['correct']:g} de {measurement['total']}.")
                self.save()
        except Stop as stop:
            if stop.reason == "cancelled":
                message = (f"Cancelado a petición ({self.requested()}) durante la prueba reservada: no empezó ninguna llamada nueva y sus resultados "
                           "parciales se conservan sin puntuación final.")
            else:
                message = "Se alcanzó un límite durante la prueba reservada; sus resultados parciales no reciben puntuación final."
            self.stop(stop.reason, message)
            return
        final["status"] = "complete"
        self.result["stopReason"] = "completed"
        self.record_seconds()
        lifecycle.finish(self.job, "completed", reason="completed")
        self.job["phase"] = None
        _event(self.job, "completed", "Trabajo completado.")
        self.save()

    def stop(self, reason: str, message: str) -> None:
        """End the attempt at a safe point: ``cancelled`` if someone asked, ``stopped`` by a limit. The job keeps the phase it reached."""
        self.result["stopReason"] = reason
        self.record_seconds()
        lifecycle.finish(self.job, "cancelled" if reason == "cancelled" else "stopped", reason=reason)
        _event(self.job, self.job["status"], message)
        self.save()


class _Requests:
    """What a case receives to call models: each call under the attempt's limits. An agent's adapter also stops at this safe point before
    each command it runs, and bounds the command by the seconds the attempt has left."""

    def __init__(self, run: _Run) -> None:
        self.run = run

    def __call__(self, role: str, messages: list[dict[str, str]], max_tokens: int | None = None) -> dict[str, Any]:
        return self.run.call(role, messages, max_tokens)

    def time_left(self) -> float:
        return self.run.deadline - time.monotonic()

    def check(self) -> None:
        """A safe point: raise :class:`Stop` if someone asked to cancel the attempt or its time ran out."""
        self.run.check()


def detached_launcher(home: Path, data_dir: Path) -> Launcher:
    """Run ``gepa job run <id>`` in a separate process that outlives the caller (for example an MCP server)."""

    def launch(job_id: str) -> None:
        logs = Path(data_dir) / JOB_FILES
        logs.mkdir(parents=True, exist_ok=True)
        command = [sys.executable, "-m", "gepa_engine", "--home", str(home), "job", "run", job_id, "--json"]
        with open(logs / f"{job_id}.log", "ab") as log:
            if sys.platform == "win32":
                subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, close_fds=True,
                                 creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS)
            else:
                subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, close_fds=True, start_new_session=True)

    return launch
