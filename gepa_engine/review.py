"""Review of a job from its persisted evidence: the report the ``review-gepa-results`` skill interprets.

Nothing here calls a model or reads today's settings: every figure comes from the job record, its
sealed dataset and the other jobs of the same data folder. Completeness is recomputed from the case
IDs each candidate actually has, never taken from a stored status. The original is compared with up
to five candidates on the same cases; a partial evaluation gets no percentage. The recommendation is
to keep the original unless the candidate chosen on validation also beats it on a complete reserved
test that no earlier job of this folder had already observed. A job prepared with an experiment
template also carries, frozen in its manifest, that template's rules to name and explain its
results; they never change a figure or the recommendation.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from typing import Any

from . import datasets
from .lifecycle import NORMAL_SEARCH_ENDS

REVIEW_FORMAT = "gepa-review-v1"
MAX_COMPARED = 5  # candidates compared besides the original
PARTITIONS = {"validation": "val", "test": "test"}  # report name -> dataset partition
ROLE_NAMES = {"decider": "decisor", "executor": "ejecutor", "reflection": "reflexión", "judge": "juez"}  # how the report names each model role
TEMPLATE_IDENTITY = ("templateId", "path", "name", "version", "sha256")  # what a review shows of a job's template: ``templateId`` only in one from the database
OBSERVED_SCOPE = ("Se consultan los trabajos de esta carpeta de datos cuya prueba reservada empezó antes de crear este trabajo. "
                  "Un caso se reconoce por su entrada, sin distinguir mayúsculas, tildes ni espacios repetidos o en los extremos, aunque cambie su id; "
                  "una entrada reescrita con otras palabras no se reconoce.")


class ReviewError(ValueError):
    """A review request that names candidates or cases the job does not have."""

    def __init__(self, message: str, code: str) -> None:
        super().__init__(message)
        self.code = code


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def stored_artifact(candidate: Mapping[str, Any]) -> Any:
    """A candidate's artifact as its job stored it; jobs recorded before Skills existed kept it under ``policy``."""
    return candidate["artifact"] if "artifact" in candidate else candidate.get("policy")


def _number(value: float) -> int | float:
    return int(value) if float(value).is_integer() else value


def _known_sum(values: list[Any]) -> float | None:
    """Sum of costs that were all reported; one unknown value makes the total unknown, never a silent zero."""
    return sum(values) if values and all(_finite(value) and value >= 0 for value in values) else None


def _submetrics(records: Sequence[Mapping[str, Any]]) -> dict[str, dict[str, Any]]:
    """Each submetric over some cases: a check as cases that passed it, a score (a judge's, a criterion's) as its mean."""
    values: dict[str, list[Any]] = {}
    for record in records:
        for key, value in (record.get("submetrics") or {}).items():
            values.setdefault(key, []).append(value)
    summary = {}
    for key in sorted(values):
        if all(isinstance(value, bool) for value in values[key]):
            summary[key] = {"passed": sum(values[key]), "evaluated": len(values[key])}
        else:
            scores = [value for value in values[key] if _finite(value)]
            summary[key] = {"mean": math.fsum(scores) / len(scores) if scores else None, "evaluated": len(scores)}
    return summary


def _flag_count(records: Sequence[Mapping[str, Any]], key: str) -> dict[str, int] | None:
    """How many cases have ``key`` true among those that record it; ``None`` when none does (the evaluator has no such notion)."""
    flags = [record[key] for record in records if isinstance(record.get(key), bool)]
    return {"passed": sum(flags), "evaluated": len(flags)} if flags else None


def check_names(records: Sequence[Mapping[str, Any]]) -> list[str]:
    """Submetrics that are checks (true or false in every case), as opposed to scores such as a judge's."""
    values: dict[str, bool] = {}
    for record in records:
        for key, value in (record.get("submetrics") or {}).items():
            values[key] = values.get(key, True) and isinstance(value, bool)
    return sorted(key for key, boolean in values.items() if boolean)


def _usage(records: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Token totals of some cases; a count one case did not report makes that total unknown."""
    if not records:
        return None
    usages: list[dict[str, Any]] = [usage if isinstance(usage, dict) else {} for usage in (record.get("usage") for record in records)]
    keys = sorted({key for usage in usages for key in usage})
    return {key: sum(usage[key] for usage in usages) if all(_finite(usage.get(key)) for usage in usages) else None for key in keys}


class _Evidence:
    """The job's stored records, indexed for the report."""

    def __init__(self, job: Mapping[str, Any], dataset_cases: Sequence[Mapping[str, Any]]) -> None:
        self.job = job
        self.manifest = job["manifest"]
        self.result = job.get("result") or {}
        self.selection = self.result.get("selection")
        self.baseline_id = self.result.get("baselineId")
        self.candidates = {candidate["id"]: candidate for candidate in self.result.get("candidates", [])}
        self.splits = self.manifest["dataset"]["splits"]
        self.higher_is_better = self.manifest["evaluator"].get("higherIsBetter", True) is not False
        self.case_ids = {case_id for members in self.splits.values() for case_id in members}
        self.cases = {case["id"]: case for case in dataset_cases if case.get("id") in self.case_ids}
        for candidate in self.candidates.values():  # a dataset record that is gone still leaves the evaluated cases
            for _, records in self.phases(candidate):
                for record in records:
                    self.cases.setdefault(record["caseId"], {"id": record["caseId"], "split": record.get("split"), "input": record.get("input"),
                                                             "expected": record.get("expected"), **({"rubric": record["rubric"]} if record.get("rubric") else {})})
        self.measures = {identifier: {name: self.measure(candidate, name) for name in PARTITIONS} for identifier, candidate in self.candidates.items()}

    @staticmethod
    def phases(candidate: Mapping[str, Any]) -> list[tuple[str, list[dict[str, Any]]]]:
        """Where each evaluation of a candidate is stored: train minibatches of the search, full validation, reserved test."""
        return [("search", candidate.get("searchCases") or []), ("validation", (candidate.get("validation") or {}).get("cases") or []),
                ("test", (candidate.get("test") or {}).get("cases") or [])]

    def records(self, candidate: Mapping[str, Any], name: str) -> dict[str, dict[str, Any]]:
        return {record["caseId"]: record for record in (candidate.get(name) or {}).get("cases") or []}

    def measure(self, candidate: Mapping[str, Any], name: str) -> dict[str, Any]:
        """Score of one candidate on one partition, computed from the case IDs it has: complete only if every case is in."""
        case_ids = self.splits[PARTITIONS[name]]
        stored = self.records(candidate, name)
        evaluated = [stored[case_id] for case_id in case_ids if case_id in stored]
        complete = bool(case_ids) and len(evaluated) == len(case_ids) and all(_finite(record.get("score")) for record in evaluated)
        correct = math.fsum(record["score"] for record in evaluated if _finite(record.get("score")))  # exact for fractional case scores
        by_expected: dict[str, dict[str, Any]] | None = None
        if evaluated and all(isinstance(record.get("expected"), str) for record in evaluated):
            by_expected = {}
            for record in evaluated:
                bucket = by_expected.setdefault(record["expected"], {"correct": 0, "evaluated": 0})
                bucket["correct"] = _number(bucket["correct"] + (record["score"] if _finite(record.get("score")) else 0))
                bucket["evaluated"] += 1
        return {"partition": PARTITIONS[name], "status": "complete" if complete else "partial" if evaluated else "not-evaluated",
                "evaluated": len(evaluated), "total": len(case_ids), "correct": _number(correct), "score": correct / len(case_ids) if complete else None,
                # Secondary metrics: each submetric, cases that met every hard requirement, and task accuracy (every check passed) where it exists.
                "submetrics": _submetrics(evaluated), "requirementsMet": _flag_count(evaluated, "requirementsMet"),
                "taskAccuracy": _flag_count(evaluated, "taskSuccess"), "byExpected": by_expected, "usage": _usage(evaluated),
                "costUsd": _known_sum([record.get("costUsd") for record in evaluated])}

    def better(self, first: float, second: float) -> bool:
        return first > second if self.higher_is_better else first < second

    def versus_original(self, candidate: Mapping[str, Any], name: str) -> dict[str, list[str]] | None:
        """Cases where the candidate scores better or worse than the original, among the cases both have."""
        original = self.records(self.candidates[self.baseline_id], name)
        mine = self.records(candidate, name)
        common = [case_id for case_id in self.splits[PARTITIONS[name]] if case_id in original and case_id in mine
                  and _finite(original[case_id].get("score")) and _finite(mine[case_id].get("score"))]
        if not common:
            return None
        return {"better": [case_id for case_id in common if self.better(mine[case_id]["score"], original[case_id]["score"])],
                "worse": [case_id for case_id in common if self.better(original[case_id]["score"], mine[case_id]["score"])]}


def _compared(evidence: _Evidence, candidate_ids: Sequence[str] | None) -> list[str]:
    """The original first, then the requested candidates, or by default the finalists (or the first proposals if none were fixed)."""
    if evidence.baseline_id is None:
        if candidate_ids:
            raise ReviewError("El trabajo no llegó a registrar candidatos.", "candidate-not-found")
        return []
    if candidate_ids is None:
        pool = evidence.selection["finalistIds"] if evidence.selection else list(evidence.candidates)
        others = [identifier for identifier in pool if identifier != evidence.baseline_id][:MAX_COMPARED]
        return [evidence.baseline_id, *others]
    if not isinstance(candidate_ids, (list, tuple)) or not all(isinstance(identifier, str) for identifier in candidate_ids):
        raise ReviewError("Los candidatos a comparar deben ser una lista de id.", "invalid-request")
    if len(set(candidate_ids)) != len(candidate_ids):
        raise ReviewError("La lista de candidatos repite un id.", "invalid-request")
    unknown = [identifier for identifier in candidate_ids if identifier not in evidence.candidates]
    if unknown:
        raise ReviewError(f"El trabajo no tiene los candidatos: {', '.join(unknown)}. Consulta sus id con 'gepa job show'.", "candidate-not-found")
    others = [identifier for identifier in candidate_ids if identifier != evidence.baseline_id]
    if len(others) > MAX_COMPARED:
        raise ReviewError(f"Se comparan como máximo {MAX_COMPARED} candidatos además del original; se pidieron {len(others)}.", "too-many-candidates")
    return [evidence.baseline_id, *others]


def _target(manifest: Mapping[str, Any]) -> dict[str, Any]:
    models = {role: {key: model.get(key) for key in ("connection", "name", "provider", "url", "model", "settingsRole", "sampling")}
              for role, model in manifest["models"].items()}
    executor_role = next((role for role, model in models.items() if model["settingsRole"] == "executor"), next(iter(models)))
    executor, adapter, evaluator = models[executor_role], manifest["adapter"], manifest["evaluator"]
    judge = models.get("judge")
    judged = f"; juzgada por el juez {judge['model']} (conexión {judge['connection']}, proveedor {judge['provider']})" if judge else ""
    execution = manifest.get("execution")  # jobs frozen before it was recorded do not have it
    if execution is None:
        how = "el manifiesto no registra agente, herramientas ni entorno"
    else:
        agent = f"agente {execution['agent']}" if execution["agent"] else "sin agente"
        tools = f"herramientas {', '.join(execution['tools'])}" if execution["tools"] else "ni herramientas" if not execution["agent"] else "sin herramientas"
        how = f"{agent} {tools}, en {execution['platform']} con Python {execution['python']}"
    code = f" (sha256 {adapter['sha256'][:12]})" if adapter.get("sha256") else ""  # an adapter an agent prepared is identified by its folder's hash
    scope = (f"La evidencia vale para el {ROLE_NAMES.get(executor_role, executor_role)} {executor['model']} (conexión {executor['connection']}, proveedor {executor['provider']}; "
             f"{how}{judged}) con el adaptador {adapter['name']} v{adapter['version']}{code}, el evaluador {evaluator['name']} v{evaluator['version']} y el contrato fijo del manifiesto; "
             "no se afirma que se transfiera a otros modelos, agentes o contratos.")
    return {**models, "executorRole": executor_role, "execution": execution, "adapter": adapter, "evaluator": evaluator, "fixedContract": manifest["fixedContract"],
            "mutableSurface": manifest["mutableSurface"], "engine": manifest["engine"], "scope": scope}


def _observed(job: Mapping[str, Any], evidence: _Evidence, history: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Earlier jobs of this data folder that had already evaluated some of this job's reserved test cases."""
    test_ids = evidence.splits["test"]
    wanted = {case_id: datasets.normalized_input(evidence.cases[case_id]["input"]) for case_id in test_ids
              if case_id in evidence.cases and evidence.cases[case_id].get("input") is not None}
    created = datetime.fromisoformat(job["createdAt"])
    found, seen_inputs = [], set()
    for other in history:
        selection = (other.get("result") or {}).get("selection")
        if other.get("id") == job["id"] or not selection or datetime.fromisoformat(selection["frozenAt"]) >= created:
            continue
        seen = {datasets.normalized_input(record["input"]) for candidate in other["result"].get("candidates", [])
                for record in (candidate.get("test") or {}).get("cases") or [] if record.get("input") is not None}
        overlap = set(wanted.values()) & seen
        if overlap:
            found.append({"jobId": other["id"], "name": other.get("name"), "finalCheckStartedAt": selection["frozenAt"],
                          "cases": sum(value in overlap for value in wanted.values())})
            seen_inputs |= overlap
    found.sort(key=lambda item: datetime.fromisoformat(item["finalCheckStartedAt"]))
    return {"observed": bool(found), "cases": sum(value in seen_inputs for value in wanted.values()), "total": len(test_ids), "jobs": found, "scope": OBSERVED_SCOPE}


def _completeness(job: Mapping[str, Any], evidence: _Evidence) -> dict[str, Any]:
    finalists = evidence.selection["finalistIds"] if evidence.selection else []
    tests = [evidence.measures[identifier]["test"]["status"] for identifier in finalists]
    final = "not-run" if not tests or all(status == "not-evaluated" for status in tests) else "complete" if all(status == "complete" for status in tests) else "partial"
    validated = all(evidence.measures[identifier]["validation"]["status"] == "complete" for identifier in finalists)
    complete = job["status"] == "completed" and final == "complete" and validated
    reason = evidence.result.get("stopReason") or (job.get("error") or {}).get("code") or job["status"]
    message = ("Evaluación completa: el original y los finalistas tienen validación y prueba reservada sobre todos sus casos." if complete else
               f"Evaluación incompleta (trabajo {job['status']}, motivo {reason}; prueba reservada: {final}): "
               "los resultados parciales no reciben porcentaje ni ganador.")
    return {"status": "complete" if complete else "incomplete", "jobStatus": job["status"], "phase": job.get("phase"), "finalCheck": final,
            "searchStopReason": evidence.result.get("searchStopReason"), "stopReason": evidence.result.get("stopReason"),
            "error": job.get("error"), "message": message}


def _score_text(measure: Mapping[str, Any]) -> str:
    return f"{measure['correct']:g}/{measure['total']}"


def _recommendation(evidence: _Evidence, complete: bool, observed: Mapping[str, Any], executor: str) -> dict[str, Any]:
    def keep(reason: str, message: str) -> dict[str, Any]:
        return {"action": "keep-original", "candidateId": None, "reason": reason, "message": "Conservar el original: " + message}

    if not complete or evidence.selection is None:
        return keep("incomplete", "la evaluación no está completa, así que ningún candidato demuestra una mejora. Las variantes siguen disponibles para inspeccionarlas.")
    if evidence.selection["selectedIsOriginal"]:
        return keep("original-selected", f"ningún candidato superó al original en la validación (criterio aprobado: {evidence.selection['rule']}).")
    selected_id, baseline_id = evidence.selection["selectedId"], evidence.baseline_id
    label = evidence.candidates[selected_id]["label"]
    selected, original = evidence.measures[selected_id], evidence.measures[baseline_id]
    test = f"{_score_text(selected['test'])} frente a {_score_text(original['test'])}"
    if not evidence.better(selected["test"]["score"], original["test"]["score"]):
        return keep("no-test-improvement", f"{label} ({selected_id}) ganó en la validación ({_score_text(selected['validation'])} frente a "
                                           f"{_score_text(original['validation'])}), pero no supera al original en la prueba reservada ({test}).")
    if observed["observed"]:
        jobs = ", ".join(item["jobId"] for item in observed["jobs"])
        return keep("test-previously-observed", f"{label} ({selected_id}) supera al original en la prueba reservada ({test}), pero {observed['cases']} de "
                                                f"{observed['total']} casos de esa prueba ya se habían observado en {jobs}: no es evidencia independiente nueva. "
                                                "El candidato sigue disponible para inspeccionarlo y exportarlo de forma explícita.")
    return {"action": "adopt-candidate", "candidateId": selected_id, "reason": "improved",
            "message": f"{label} ({selected_id}) supera al original en la validación ({_score_text(selected['validation'])} frente a "
                       f"{_score_text(original['validation'])}) y en la prueba reservada ({test}) con {executor}. "
                       "Exportarlo es una decisión explícita: no se instala sobre el original."}


def _presentation(manifest: Mapping[str, Any]) -> dict[str, Any] | None:
    """How to present the results, from the template the job's dataset was prepared with (frozen in its manifest); ``None`` without one.

    A template folder is named by its path, name, version and hash; one saved in the engine's database before templates were folders, by its id.
    """
    template = manifest.get("template")
    if template is None:
        return None
    return {"template": {key: template[key] for key in TEMPLATE_IDENTITY if key in template}, **template["presentation"]}


def template_label(template: Mapping[str, Any]) -> str:
    """How a report names the template of a job: its name, version and hash, and its id when it was saved in the engine's database."""
    where = f"{template['templateId']} " if "templateId" in template else ""
    return f"{where}«{template['name']}» v{template['version']} (sha256 {template['sha256'][:12]})"


def _limits(evidence: _Evidence, scope: str, completeness: Mapping[str, Any], observed: Mapping[str, Any], cost: Any,
            presentation: Mapping[str, Any] | None) -> list[str]:
    limits = [scope, "La validación sirvió para elegir al seleccionado: su puntuación no es independiente. La prueba reservada es la comprobación independiente."]
    if presentation is not None:
        limits.append(f"La presentación sigue la plantilla {template_label(presentation['template'])}: solo nombra y explica. "
                      "Los números, la selección y la recomendación salen de la evidencia de este trabajo, no de trabajos anteriores de la plantilla.")
    if observed["observed"]:
        jobs = ", ".join(item["jobId"] for item in observed["jobs"])
        limits.append(f"La prueba reservada comparte {observed['cases']} de {observed['total']} casos con trabajos anteriores ({jobs}): no es evidencia independiente nueva.")
    else:
        limits.append("Ningún trabajo anterior de esta carpeta de datos había evaluado casos de prueba con estas entradas; no se consultan otras carpetas ni herramientas, "
                      "y una entrada reescrita con otras palabras no se reconoce.")
    if completeness["status"] != "complete":
        limits.append(completeness["message"])
    stop = evidence.result.get("searchStopReason")
    if evidence.selection and stop not in (None, *NORMAL_SEARCH_ENDS):
        validated = sum(evidence.measures[identifier]["validation"]["status"] == "complete" for identifier in evidence.candidates)
        limits.append(f"La búsqueda se detuvo antes de terminar (motivo {stop}): la selección se hizo entre los {validated} candidatos con validación "
                      "completa y no se exploraron más propuestas.")
    continued = [attempt["attempt"] for attempt in evidence.job.get("attempts", []) if attempt["kind"] == "continue-search" and "search" in attempt["phases"]]
    if continued:
        limits.append(f"La búsqueda se continuó desde el último estado guardado de GEPA (intento {', '.join(map(str, continued))}): lo hecho después de "
                      "ese estado se repitió como gasto nuevo, y GEPA no guarda su generador aleatorio, así que la continuación usó otra vez la semilla "
                      "del manifiesto: no es idéntica a una búsqueda sin corte.")
    short = [attempt["attempt"] for attempt in evidence.job.get("attempts", []) if attempt.get("countsLowerBound")]
    if short:
        limits.append(f"Los contadores del intento {', '.join(map(str, short))} son un mínimo: su proceso terminó sin registrar lo último que hizo, "
                      "así que pudo hacer más llamadas y más evaluaciones de la prueba reservada de las que constan. Las evaluaciones de búsqueda "
                      "constan todas.")
    total = len(evidence.splits["test"])
    if total:
        limits.append(f"La prueba reservada tiene {total} casos: cada caso pesa 1/{total} de la métrica principal.")
    notes = {model.get("sampling") for model in evidence.manifest["models"].values()} - {None}
    limits.extend(f"{note} Repetir la evaluación puede dar otro resultado." for note in sorted(notes))
    judge = evidence.manifest["models"].get("judge")
    if judge:
        limits.append(f"El score de la rúbrica lo da el juez {judge['model']}: otro juez puede puntuar distinto. "
                      "Un requisito obligatorio incumplido deja el caso en 0 sea cual sea su nota.")
    if not evidence.manifest["dataset"].get("reviewedAllCases"):
        limits.append("Al aprobar el dataset no se declaró la revisión completa de los casos.")
    if cost is None:
        limits.append("El coste total es desconocido: algún proveedor no informó coste o una llamada falló.")
    return limits


def _evaluation(candidate: Mapping[str, Any], phase: str, record: Mapping[str, Any]) -> dict[str, Any]:
    # A Skill case also keeps each check, the files it produced (effects), its session's actions and, with a judge, its verdict;
    # a case of an agent's adapter keeps what it observed and its steps (model calls, commands and notes).
    extra = {key: record[key] for key in ("requirementsMet", "taskSuccess", "checks", "judge", "effects", "session", "observation", "steps") if key in record}
    return {"candidateId": candidate["id"], "label": candidate["label"], "phase": phase, "output": record.get("output"), "decision": record.get("decision"),
            "score": record.get("score"), "submetrics": record.get("submetrics"), "feedback": record.get("feedback"), "error": record.get("error"),
            **extra, "trace": {"latencyMs": record.get("latencyMs"), "usage": record.get("usage"), "costUsd": record.get("costUsd")}}


def _case_info(evidence: _Evidence, case_id: str) -> dict[str, Any]:
    case = evidence.cases.get(case_id, {})
    split = next(split for split, members in evidence.splits.items() if case_id in members)
    return {"caseId": case_id, "split": split, "input": case.get("input"), "expected": case.get("expected"), **({"rubric": case["rubric"]} if case.get("rubric") else {}),
            "source": case.get("source")}


def _case_detail(evidence: _Evidence, case_id: Any, compared: Sequence[str]) -> dict[str, Any]:
    if not isinstance(case_id, str) or case_id not in evidence.case_ids:
        raise ReviewError(f"El dataset del trabajo no tiene el caso '{case_id}'.", "case-not-found")
    evaluations = [_evaluation(evidence.candidates[identifier], phase, record) for identifier in compared
                   for phase, records in _Evidence.phases(evidence.candidates[identifier]) for record in records if record["caseId"] == case_id]
    return {**_case_info(evidence, case_id), "evaluations": evaluations}


def _case_rows(evidence: _Evidence, compared: Sequence[str]) -> list[dict[str, Any]]:
    """One row per validation and test case with each compared candidate's result, to see where each one works or fails."""
    rows = []
    for name, split in PARTITIONS.items():
        stored = {identifier: evidence.records(evidence.candidates[identifier], name) for identifier in compared}
        for case_id in evidence.splits[split]:
            info = _case_info(evidence, case_id)
            results = [{"candidateId": identifier, "phase": name, "score": stored[identifier][case_id].get("score"),
                        # An agent's adapter makes no decision: its row has none, not an empty one.
                        **({} if "observation" in stored[identifier][case_id] else {"decision": stored[identifier][case_id].get("decision")}),
                        "error": stored[identifier][case_id].get("error"),
                        **({"session": stored[identifier][case_id]["session"]["status"]} if "session" in stored[identifier][case_id] else {}),
                        **{key: stored[identifier][case_id][key] for key in ("requirementsMet",) if key in stored[identifier][case_id]}}
                       for identifier in compared if case_id in stored[identifier]]
            rows.append({**{key: info[key] for key in ("caseId", "split", "input", "expected")}, "results": results})
    return rows


def build(job: Mapping[str, Any], dataset_cases: Sequence[Mapping[str, Any]], history: Iterable[Mapping[str, Any]], *, artifact_key: str = "policy",
          candidate_ids: Sequence[str] | None = None, case_id: str | None = None, cases: bool = False) -> dict[str, Any]:
    """The review report of ``job``: ``case_id`` adds every stored evaluation of one case, ``cases`` one row per validation and test case.

    ``artifact_key`` names each candidate's artifact in the report (``policy`` or ``skill``).
    """
    evidence = _Evidence(job, dataset_cases)
    manifest, result = evidence.manifest, evidence.result
    compared = _compared(evidence, candidate_ids)
    target = _target(manifest)
    role = target["executorRole"]
    executor = f"el {ROLE_NAMES.get(role, role)} {target[role]['model']}"
    observed = _observed(job, evidence, history)
    completeness = _completeness(job, evidence)
    recommendation = _recommendation(evidence, completeness["status"] == "complete", observed, executor)
    finalists = evidence.selection["finalistIds"] if evidence.selection else []
    exportable = job["status"] == "completed" and (result.get("finalCheck") or {}).get("status") == "complete"
    candidates = []
    for identifier in compared:
        candidate = evidence.candidates[identifier]
        is_original = identifier == evidence.baseline_id
        can_export = exportable and not is_original and identifier in finalists
        candidates.append({
            "id": identifier, "label": candidate["label"], "origin": candidate["origin"], "parents": candidate["parents"],
            "role": "original" if is_original else "finalist" if identifier in finalists else "candidate",
            "selected": identifier == (evidence.selection or {}).get("selectedId") and not is_original, artifact_key: stored_artifact(candidate),
            **evidence.measures[identifier],
            "vsOriginal": None if is_original else {name: evidence.versus_original(candidate, name) for name in PARTITIONS},
            "exportable": can_export,
            "export": {"cli": f"gepa job export {job['id']} --out <carpeta-nueva> --candidate {identifier}",
                       "tool": {"name": "gepa_job_export", "arguments": {"jobId": job["id"], "outDir": "<carpeta-nueva>", "candidateId": identifier}}} if can_export else None,
        })
    comparison = {}
    for name, split in PARTITIONS.items():
        statuses = {identifier: evidence.measures[identifier][name]["status"] for identifier in compared}
        comparison[name] = {"total": len(evidence.splits[split]), "sameDenominator": bool(compared) and all(status == "complete" for status in statuses.values()),
                            **{key: [identifier for identifier, status in statuses.items() if status == value]
                               for key, value in (("complete", "complete"), ("partial", "partial"), ("notEvaluated", "not-evaluated"))}}
    counts = result.get("counts") or {}
    evaluations = {"search": counts.get("searchEvaluations", 0), "final": counts.get("finalEvaluations", 0)}
    records = [record for candidate in evidence.candidates.values() for _, stored in _Evidence.phases(candidate) for record in stored]
    names = sorted({key for record in records for key in record.get("submetrics") or {}})
    proposals = result.get("proposals", [])
    cost = result.get("costUsd")
    presentation = _presentation(manifest)
    report: dict[str, Any] = {
        "format": REVIEW_FORMAT, "jobId": job["id"], "name": job["name"], "status": job["status"], "phase": job.get("phase"),
        "createdAt": job["createdAt"], "finishedAt": job.get("finishedAt"), "manifestSha256": job["manifestSha256"], "objective": manifest.get("objective"),
        "artifact": {key: manifest["artifact"].get(key) for key in ("type", "source", "sha256")},
        "dataset": {key: manifest["dataset"].get(key) for key in ("id", "sha256", "approvedAt", "reviewedAllCases")},
        "target": target,
        "primaryMetric": {"name": manifest["evaluator"]["primaryMetric"], "higherIsBetter": evidence.higher_is_better,
                          "definition": manifest["evaluator"].get("description"),
                          "caseAggregation": manifest["evaluator"].get("aggregation"),  # how one case's score is built; jobs before it was sealed lack it
                          "aggregation": "Media del score por caso sobre todos los casos de la partición; solo con cobertura completa."},
        "submetrics": names, "presentation": presentation, "selection": evidence.selection, "completeness": completeness,
        "counts": {
            "cases": manifest["dataset"]["counts"], "evaluations": evaluations, "modelCalls": counts.get("modelCalls"), "iterations": counts.get("iterations", 0),
            "proposals": {status: sum(item.get("status") == status for item in proposals) for status in ("valid", "rejected", "failed")},
            "internalChecks": {"names": check_names(records), "total": counts.get("internalChecks"),  # None: counted only by jobs run since this report exists
                               "note": "Comprobaciones dentro de cada evaluación terminada de un caso: no son casos nuevos ni cambian el denominador."},
        },
        "usage": result.get("usage"), "costUsd": cost, "cost": {"known": cost is not None, "usd": cost},
        "testObservedBefore": observed, "compared": compared, "candidates": candidates, "comparison": comparison,
        "recommendation": recommendation, "limits": _limits(evidence, target["scope"], completeness, observed, cost, presentation),
    }
    if cases:
        report["cases"] = _case_rows(evidence, compared)
    if case_id is not None:
        report["case"] = _case_detail(evidence, case_id, compared)
    return report
