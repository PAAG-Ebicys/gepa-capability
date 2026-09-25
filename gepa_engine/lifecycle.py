"""A job's life across processes: the runner's lock, cancelling from another session, attempts, accounting and what can be retried.

A job runs in one process (the CLI's, or a detached ``gepa job run``) while other sessions read it. The
runner holds an operating-system lock on the job's lock file for the whole run, and the system releases it
when the process ends for any reason (closed, killed, the computer restarted): a reader that finds a
``running`` job whose lock nobody holds knows its runner died and marks it ``interrupted``. Only the runner
writes a running job's record, because it rewrites the record whole; a cancellation is a separate small
record, tied to the attempt, that the runner reads before each model call and each case.

Each run of a job is an attempt. The first runs the search and, if a validated candidate wins, the reserved test; a retry of a job whose
selection was frozen evaluates only the reserved test cases still pending, if needed, without GEPA, and a retry of a search
cut short either closes with the candidates validated so far or continues GEPA from the state it last saved.
Every attempt keeps what it spent per phase, so the search limit never hides the cost of the reserved test or of
its retries.
"""

from __future__ import annotations

import sys
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import IO, Any

from .storage import Storage

ACTIVE = ("queued", "running")
RETRY_KINDS = ("continue-search", "final-retry", "run")  # the kinds of attempt a retry creates, as ``recovery`` offers them
CANCEL_KIND = "job-cancel"  # records of cancellations requested for an attempt, apart from the job the runner rewrites
SPEND_KIND = "job-search-spend"  # the search evaluations each job started, written before each one: a few bytes, apart from the job record
USAGE_KEYS = ("prompt_tokens", "completion_tokens", "total_tokens")
INTERRUPTED_MESSAGE = ("El proceso del trabajo terminó sin cerrarlo (se cerró, falló o se reinició el equipo). La evidencia guardada se conservó; "
                       "lo ocurrido después del último guardado (al menos el caso en curso) no quedó registrado: las llamadas de este intento y "
                       "sus evaluaciones de la prueba reservada son un mínimo, y el uso y el coste pasan a desconocidos. Las evaluaciones de búsqueda "
                       "sí constan todas, también la que estaba en curso, porque se anotan antes de empezar cada caso.")
NORMAL_SEARCH_ENDS = ("completed", "budget", "max_proposals", "validation_target")  # a search that ended by its own limits, not cut short


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


# The runner's lock --------------------------------------------------------------------------


class RunnerLock:
    """An exclusive lock on a job's lock file that the operating system releases when the holding process ends."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: IO[bytes] | None = None

    def acquire(self) -> bool:
        """Take the lock without waiting; ``False`` if another handle, of this process or another one, holds it."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = open(self.path, "a+b")  # held until release(); a+b creates the file without truncating it
        if not _try_lock(handle):
            handle.close()
            return False
        self._handle = handle
        return True

    def release(self) -> None:
        if self._handle is not None:
            _unlock(self._handle)
            self._handle.close()
            self._handle = None


def runner_alive(path: Path) -> bool:
    """Whether a process holds the lock at ``path``, that is, a runner still executing its job."""
    if not path.exists():
        return False
    probe = RunnerLock(path)
    if not probe.acquire():
        return True
    probe.release()
    return False


if sys.platform == "win32":
    import msvcrt

    def _try_lock(handle: IO[bytes]) -> bool:
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    def _unlock(handle: IO[bytes]) -> None:
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

else:
    import fcntl

    def _try_lock(handle: IO[bytes]) -> bool:
        try:  # flock, not lockf: two handles of the same process exclude each other, as on Windows
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return False
        return True

    def _unlock(handle: IO[bytes]) -> None:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


# Cancellation ---------------------------------------------------------------------------------


def _cancel_key(job_id: str, attempt: int) -> str:
    return f"{job_id}#{attempt}"


def request_cancel(store: Storage, job_id: str, attempt: int) -> None:
    """Ask the runner to stop the attempt at its next safe point; a repeated request keeps the first."""
    store.insert(CANCEL_KIND, _cancel_key(job_id, attempt), {"at": now()})


def cancel_requested(store: Storage, job_id: str, attempt: int) -> dict[str, Any] | None:
    """The cancellation requested for this attempt (``{"at": ...}``), if any; one for an earlier attempt never stops a retry."""
    request = store.get(CANCEL_KIND, _cancel_key(job_id, attempt))
    return request if isinstance(request, dict) else None


# Search spend ---------------------------------------------------------------------------------


def record_search_spend(store: Storage, job_id: str, evaluations: int) -> None:
    """Record, before a search evaluation starts, how many the job has started: the job record is rewritten at most once a second, this
    small one before every case, so a runner that dies never leaves the search budget counting less than it spent."""
    store.put(SPEND_KIND, job_id, {"searchEvaluations": evaluations})


def search_spend(store: Storage, job_id: str) -> int:
    """The search evaluations the job had started by its last record; 0 before its first."""
    record = store.get(SPEND_KIND, job_id)
    return int(record["searchEvaluations"]) if isinstance(record, dict) else 0


# Attempts, events and accounting --------------------------------------------------------------


def new_attempt(number: int, kind: str, requested_at: str) -> dict[str, Any]:
    """An attempt waiting for its runner: ``run`` executes the whole job, ``final-retry`` closes a frozen selection and any pending
    reserved test cases, and ``continue-search`` resumes a cut-short search from GEPA's saved state before applying the selection rule."""
    return {"attempt": number, "kind": kind, "status": "queued", "phase": None, "requestedAt": requested_at, "startedAt": None, "finishedAt": None,
            "stopReason": None, "error": None, "phases": {}}


def attempt_of(job: dict[str, Any]) -> dict[str, Any]:
    """The job's current attempt; a job recorded before attempts existed had exactly one."""
    return job.setdefault("attempts", [new_attempt(1, "run", job["createdAt"])])[-1]  # type: ignore[no-any-return]


def new_meter(max_evaluations: int, roles: Mapping[str, Any]) -> dict[str, Any]:
    """What an attempt spent in one phase: evaluations against their limit, model calls per role, tokens, cost and seconds."""
    return {"evaluations": 0, "maxEvaluations": max_evaluations, "modelCalls": dict.fromkeys(roles, 0), "usage": dict.fromkeys(USAGE_KEYS, 0),
            "costUsd": 0.0, "seconds": 0.0}


def event(job: dict[str, Any], kind: str, message: str) -> None:
    """Append a durable event with the phase the job is in and its attempt."""
    job["events"].append({"at": now(), "kind": kind, "phase": job.get("phase"), "attempt": attempt_of(job)["attempt"], "message": message})


def settle(job: dict[str, Any], status: str) -> None:
    """Leave nothing ``running`` in a stopped job: the reserved test takes the job's status, and an unfinished finalist test becomes partial."""
    result = job.get("result")
    if not result:
        return
    final = result.get("finalCheck")
    if final and final["status"] == "running":
        final["status"] = status
    for candidate in result["candidates"]:
        test = candidate.get("test")
        if test and test["status"] == "running":
            test["status"] = "partial" if test["cases"] else "pending"


def finish(job: dict[str, Any], status: str, *, reason: str | None = None, error: dict[str, str] | None = None) -> None:
    """Close the job and its current attempt with ``status``; both keep the phase they reached. A search cut short records why."""
    settle(job, status)
    result = job.get("result")
    if result and job["phase"] == "search" and result.get("searchStopReason") is None:
        result["searchStopReason"] = status
    at = now()
    job.update(status=status, finishedAt=at, error=error)
    attempt_of(job).update(status=status, phase=job["phase"], finishedAt=at, stopReason=reason, error=error)


def interrupt(job: dict[str, Any], *, search_spent: int = 0) -> None:
    """Close a job whose runner died. Its evidence stays; what happened after its last save is lost (at least the case in progress),
    so the attempt's counts are a lower bound, and usage and cost become unknown for the phase in progress and for the job's totals.
    A phase the attempt had finished was saved whole when the next one began: its accounting is kept. The search evaluations are exact:
    ``search_spent`` (from :func:`search_spend`) counts every one the runner started, the case it was running included."""
    attempt = attempt_of(job)
    result = job.get("result")
    if result:
        result["usage"] = dict.fromkeys(result["usage"])
        result["costUsd"] = None
        unsaved = search_spent - result["counts"]["searchEvaluations"]
        if unsaved > 0 and "search" in attempt["phases"]:  # started after the job's last save, so in this attempt's search
            result["counts"]["searchEvaluations"] += unsaved
            attempt["phases"]["search"]["evaluations"] += unsaved
    meter = attempt["phases"].get(job["phase"])
    if meter is not None:
        meter.update(usage=dict.fromkeys(meter["usage"]), costUsd=None, countsLowerBound=True)
    attempt.update(recordedUntil=job["updatedAt"], countsLowerBound=True)
    finish(job, "interrupted", error={"code": "interrupted", "message": INTERRUPTED_MESSAGE})
    event(job, "interrupted", INTERRUPTED_MESSAGE)


def consumption(job: Mapping[str, Any]) -> dict[str, Any]:
    """What the job spent per phase over all its attempts: evaluations started (failed ones included), calls per role, tokens, cost and seconds.

    The search is measured against the budget its manifest froze. The reserved test shows how many of the results it needs are in
    (``resolved`` of ``required``, finalists times test cases); each attempt's own limit is in ``attempts``. A phase that never started
    is ``None``; cost is unknown when a call did not report it; ``countsLowerBound`` says an attempt ended without recording its last calls.
    """
    phases: dict[str, dict[str, Any]] = {}
    for attempt in job.get("attempts", []):
        for name, meter in attempt["phases"].items():
            total = phases.setdefault(name, {"evaluations": 0, "attempts": 0, "modelCalls": {}, "usage": dict.fromkeys(USAGE_KEYS, 0), "costUsd": 0.0, "seconds": 0.0})
            total["evaluations"] += meter["evaluations"]
            total["attempts"] += 1
            for role, calls in meter["modelCalls"].items():
                total["modelCalls"][role] = total["modelCalls"].get(role, 0) + calls
            total["usage"] = {key: None if total["usage"][key] is None or meter["usage"].get(key) is None else total["usage"][key] + meter["usage"][key]
                              for key in USAGE_KEYS}
            total["costUsd"] = None if total["costUsd"] is None or meter["costUsd"] is None else total["costUsd"] + meter["costUsd"]
            total["seconds"] = round(total["seconds"] + meter["seconds"], 3)
    for total in phases.values():
        if not sum(total["modelCalls"].values()):
            total["costUsd"] = None
    limits = job["manifest"]["limits"]
    search, final = phases.get("search"), phases.get("final")
    if search:
        search["maxEvaluations"] = limits["maxMetricCalls"]
    if final:
        final_check = (job.get("result") or {}).get("finalCheck") or {}
        final.update(required=final_check.get("required"), resolved=final_check.get("resolved"))
    elif ((job.get("result") or {}).get("finalCheck") or {}).get("status") == "skipped":
        phases["final"] = {"evaluations": 0, "attempts": 0, "modelCalls": {}, "usage": dict.fromkeys(USAGE_KEYS, 0),
                           "costUsd": None, "seconds": 0.0, "required": 0, "resolved": 0}
        final = phases["final"]
    return {"search": search, "final": final, "attempts": len(job.get("attempts", [])), "timeLimitMinutes": limits["timeLimitMinutes"],
            "countsLowerBound": any(attempt.get("countsLowerBound") for attempt in job.get("attempts", [])),
            "note": "Cada intento tiene su propio límite de tiempo y, en la prueba reservada, como máximo una evaluación por caso pendiente al empezar; "
                    "los totales suman todos los intentos, también los que fallaron."}


# Recovery -------------------------------------------------------------------------------------


def _option(job_id: str, kind: str, message: str) -> dict[str, Any]:
    """One recovery a person can choose, with the retry that runs it."""
    return {"kind": kind, "message": message, "cli": f"gepa job retry {job_id} --kind {kind}",
            "tool": {"name": "gepa_job_retry", "arguments": {"jobId": job_id, "kind": kind}}}


def recovery(job: Mapping[str, Any]) -> dict[str, Any] | None:
    """Whether a job that stopped before completing can be retried, and with which ``kind`` of attempt; ``None`` while it is active or once
    it completed. ``options`` lists each recovery; with two, ``kind`` is ``None`` and the person chooses.

    It is decided by the phases the attempts reached and by the GEPA state the search saved, never by stored counts, which a dead runner
    leaves short. With the selection frozen, a ``final-retry`` evaluates only the pending reserved test cases. A search cut short with the
    original's validation complete can close with a ``final-retry``, which freezes the selection among the candidates validated so far, or,
    once GEPA saved its state, continue from it with a ``continue-search``. Only a job none of whose attempts began a phase (each stopped
    queued or in its preparation) called no model, so a ``run`` executes it whole.
    """
    if job["status"] in ACTIVE or job["status"] == "completed":
        return None
    job_id = job["id"]
    result = job.get("result") or {}
    candidates = {candidate["id"]: candidate for candidate in result.get("candidates", [])}
    original = candidates.get(result.get("baselineId") or "", {})
    reason = result.get("searchStopReason") or (job.get("error") or {}).get("code") or job["status"]
    if result.get("selection") is not None:
        options = [_option(job_id, "final-retry", "La búsqueda terminó y la selección está congelada: el reintento evalúa solo los casos pendientes "
                                                  "de la prueba reservada, sin repetir GEPA y sin borrar lo ya contabilizado.")]
    elif not any(attempt["phases"] for attempt in job.get("attempts", [])):
        options = [_option(job_id, "run", "Ningún intento llegó a la búsqueda, así que no se llamó a ningún modelo: el reintento ejecuta el trabajo completo.")]
    elif (original.get("validation") or {}).get("status") == "complete":
        validated = sum((candidate.get("validation") or {}).get("status") == "complete" for candidate in candidates.values())
        close = (f"Cerrar con lo validado: el reintento elige con la misma regla entre los {validated} candidatos con validación completa (el "
                 "original incluido) y evalúa solo la prueba reservada, sin continuar GEPA.")
        saved = result.get("searchState")
        if saved is None:
            options = [_option(job_id, "final-retry", f"La búsqueda se detuvo antes de terminar (motivo {reason}) sin que GEPA llegara a guardar un "
                                                      f"estado desde el que continuarla. {close} Para seguir buscando, crea un trabajo nuevo.")]
        else:
            limits = job["manifest"]["limits"]
            spent = result["counts"]["searchEvaluations"]
            resume = (f"Continuar la búsqueda desde el último estado que guardó GEPA (al empezar la iteración {saved['iterations'] + 1}, "
                      f"{saved['savedAt']}), como después de una pausa: no repite las iteraciones completas, y lo hecho después de ese estado se "
                      f"repite y cuenta como gasto nuevo. El presupuesto es del trabajo y no se reinicia: van {saved['iterations']} de "
                      f"{limits['maxProposals']} iteraciones de búsqueda y {spent} de {limits['maxMetricCalls']} evaluaciones; el tiempo es de cada intento. "
                      "Al terminar la búsqueda se congela la selección; la prueba reservada se evalúa solo si gana una mejora validada.")
            return {"retryable": True, "kind": None, "code": None, "cli": None, "tool": None,
                    "message": f"La búsqueda se detuvo antes de terminar (motivo {reason}). Pregunta a la persona cuál de las dos recuperaciones "
                               "quiere: continuar la búsqueda desde el último estado guardado de GEPA, o cerrar con los candidatos ya validados. "
                               "También puede crear un trabajo nuevo.",
                    "options": [_option(job_id, "continue-search", resume), _option(job_id, "final-retry", close)]}
    else:
        return {"retryable": False, "kind": None, "code": "search-not-recoverable", "cli": None, "tool": None, "options": [],
                "message": f"La búsqueda no terminó (motivo {reason}) sin completar la validación del original, la base de toda comparación, así "
                           "que GEPA no guardó un estado desde el que continuarla: crea un trabajo nuevo con la misma especificación. La evidencia "
                           "de este trabajo se conserva para revisarla."}
    [option] = options
    return {"retryable": True, "kind": option["kind"], "code": None, "message": option["message"], "cli": f"gepa job retry {job_id}",
            "tool": {"name": "gepa_job_retry", "arguments": {"jobId": job_id}}, "options": options}
