"""Running an adapter an agent prepares for a task the built-in adapters do not cover: its code, what it receives and what the engine checks.

``adapter.py`` defines ``execute(task)``, which applies the candidate to one case and returns what it
observed, and ``evaluate(task, observation)``, which scores that; it may also define ``check_case(case)``
and ``check(environment)``. Its folder and ``adapter.json`` are :mod:`gepa_engine.declaration`'s. The
engine keeps the rest: the artifact's mutable surface, GEPA, the partitions, the limits, the evidence and
the reflection. A job runs only the frozen copy of the adapter, and checks after each case that the copy
did not change.

Each evaluation gets a new workspace holding the candidate artifact. The engine records what
``execute`` returned, the files it created or changed and its steps: the model calls, commands and notes
of the case. It checks what ``evaluate`` returned (a score from 0 to 1, a diagnostic feedback,
submetrics) and stops the evaluation without scoring the candidate when a result breaks this contract,
the adapter's code fails, a requirement is missing, a model call fails or the task's infrastructure
fails. The adapter's code cannot hide any of these by catching it: the engine keeps the first one and
raises it afterwards.
"""

from __future__ import annotations

import hashlib
import importlib.util
import inspect
import json
import math
import os
import shutil
import subprocess
import sys
import time
import types
from collections.abc import Callable, Iterable, Mapping, Sequence
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, ClassVar, TypeGuard

from . import spaces
from .artifacts import Kind, Surface
from .datasets import SECRET_SUFFIXES, canonical_json
from .declaration import CODE, DECLARATION, AdapterError, Folder, parse_declaration, read_folder
from .encoding import storable
from .errors import ContractError, Stop, WorkspaceError
from .evaluation import JUDGE_ROLE, EvaluatorError, JudgeError, judge_messages, judge_verdict, parse_criteria
from .providers import ProviderError

CHECK_FORMAT = "gepa-adapter-check-v1"
RESULT_KEYS = frozenset({"score", "feedback", "submetrics", "requirementsMet", "taskSuccess", "error"})
CHECK_KEYS = frozenset({"requirement", "status", "code", "message", "nextStep", "resolvableBy"})
CHECK_STATUSES = ("ok", "warning", "error")
RESOLVERS = ("agent", "person")  # who can resolve a finding: the agent itself, or the person (a credential, an account, a decision)
MAX_TEXT = 2000
MAX_OBSERVATION = 100_000  # characters of an observation's JSON kept as evidence
MAX_OUTPUT = 20_000  # characters of a case's output kept as evidence
MAX_KEPT = 20_000  # characters of each file a case created or changed kept as evidence
MAX_FEEDBACK = 4000
MAX_SUBMETRICS = 100
MAX_STEPS = 100  # steps kept per evaluation
MAX_TRACED = 2000  # characters of each reply, argument or command output kept in a step
MAX_REFLECTED = 2000  # characters of a case's output shown to the reflection
MAX_COMMAND_SECONDS = 600
MAX_COMMAND_OUTPUT = 1_000_000  # characters of stdout and of stderr a command returns to the adapter
MAX_DETAIL = 300
MIN_SECRET = 4  # a shorter credential value is not searched for in the evidence: replacing it would garble the texts
HIDDEN = "[credencial oculta]"
JUDGED = "el resultado de un caso de un experimento de optimización"  # what task.judge tells the judge it values, by default
CHILD_PYTHON = {"PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8", "PYTHONDONTWRITEBYTECODE": "1"}  # UTF-8 on every system; no __pycache__ among a case's effects


class InfrastructureError(AdapterError):
    """What an adapter raises (``raise task.infrastructure_error(...)``) when the task's environment fails: never the candidate's result."""

    def __init__(self, message: str) -> None:
        super().__init__(message, "infrastructure-error")


@dataclass(frozen=True)
class Runtime:
    """Where an adapter runs: the folder of its isolated workspaces, the environment its credentials come from, and the variables holding a connection's credential."""

    workspaces: Path | None
    environ: Mapping[str, str]
    hidden: frozenset[str]


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def _finite(value: Any) -> TypeGuard[int | float]:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _described(error: BaseException) -> str:
    """An exception as ``Type: message``, short enough for an error message."""
    message = str(error).strip()
    return _clip(storable(f"{type(error).__name__}: {message}" if message else type(error).__name__), MAX_DETAIL)


# The code --------------------------------------------------------------------------------

_LOADED: dict[tuple[str, str], types.ModuleType] = {}


def _module(folder: Path, contents: Folder) -> types.ModuleType:
    """The adapter's code, compiled from its bytes into a module of its own; nothing is written to its folder (no ``__pycache__``).

    Raises whatever the code raises while it loads. A folder and content already loaded in this process reuse their module.
    """
    key = (contents.sha256, str(folder))
    if key in _LOADED:
        return _LOADED[key]
    name = f"gepa_adapter_{contents.sha256[:16]}_{len(_LOADED)}"
    module = types.ModuleType(name)
    module.__file__ = str(folder / CODE)
    sys.modules[name] = module  # dataclasses and pickling look a module up by name
    try:
        exec(compile(contents.data(CODE), module.__file__, "exec"), module.__dict__)  # nosec B102: the adapter is code the person's own agent wrote for this task
    except BaseException:
        sys.modules.pop(name, None)
        raise
    _LOADED[key] = module
    return module


def _contract_problem(module: types.ModuleType) -> str | None:
    """What ``adapter.py`` lacks of the contract, or ``None``."""
    shapes = {"execute": 1, "evaluate": 2, "check_case": 1, "check": 1}  # function -> positional arguments it receives
    for function, arguments in shapes.items():
        value = getattr(module, function, None)
        if value is None and function in ("execute", "evaluate"):
            return f"{CODE} debe definir execute(task) y evaluate(task, observation); falta {function}."
        if value is None:
            continue
        if not callable(value):
            return f"{function} de {CODE} debe ser una función."
        try:
            inspect.signature(value).bind(*range(arguments))
        except TypeError:
            return f"{function} de {CODE} debe aceptar {arguments} argumento{'s' if arguments > 1 else ''}."
        except ValueError:
            continue  # a callable without an inspectable signature: tried when it runs
    return None


def _loaded(folder: Path, contents: Folder) -> types.ModuleType:
    try:
        module = _module(folder, contents)
    except (Exception, SystemExit) as error:
        raise AdapterError(f"{CODE} no se pudo cargar ({_described(error)}); ejecuta 'gepa adapter check' para ver el diagnóstico.", "adapter-error") from None
    problem = _contract_problem(module)
    if problem:
        raise AdapterError(problem, "invalid-adapter")
    return module


# What the adapter's code receives --------------------------------------------------------


def _secret_name(name: str) -> bool:
    """Whether an environment variable's name looks like a credential (``OPENROUTER_API_KEY``, ``GH_TOKEN``, ``DB_PASSWORD``)."""
    compact = "".join(ch for ch in name.lower() if ch.isalnum())
    return compact.endswith((*SECRET_SUFFIXES, "token", "key", "pass", "pwd")) or "secret" in compact or "password" in compact


def child_environment(base: Mapping[str, str], hidden: Iterable[str]) -> dict[str, str]:
    """``base`` without credentials: the variables the engine knows hold one, and any whose name looks like one."""
    drop = {name.upper() for name in hidden}
    return {name: value for name, value in base.items() if name.upper() not in drop and not _secret_name(name)}


def _decoded(data: bytes | str | None) -> str:
    if data is None:
        return ""
    text = data if isinstance(data, str) else data.decode("utf-8", errors="replace")
    return _clip(storable(text), MAX_COMMAND_OUTPUT)


def _messages(value: Any) -> list[dict[str, str]]:
    if not isinstance(value, list) or not value or not all(isinstance(item, dict) and set(item) == {"role", "content"} and item["role"] in ("system", "user", "assistant")
                                                            and isinstance(item["content"], str) for item in value):
        raise AdapterError("task.model: 'messages' debe ser una lista no vacía de {\"role\": \"system\" | \"user\" | \"assistant\", \"content\": \"<texto>\"}.", "adapter-error")
    return [{"role": item["role"], "content": storable(item["content"])} for item in value]


class _Session:
    """The model calls and commands of one evaluation: counted by the engine, metered and traced.

    An engine stop (time or budget), a failed model call, a broken contract or an infrastructure failure
    is kept as it is raised, so the engine raises it again after the adapter's function returns, even
    if the adapter caught it.
    """

    def __init__(self, request: Callable[..., Mapping[str, Any]] | None, *, roles: Mapping[str, int], workspace: Path, adapter_dir: Path,
                 credentials: Mapping[str, str | None], hidden: frozenset[str]) -> None:
        self.request = request
        self.roles = roles  # role -> its declared output tokens
        self.workspace = workspace
        self.adapter_dir = adapter_dir
        self.credentials = credentials  # declared credential -> its value, None when missing
        self.hidden = hidden
        self.phase = "execute"
        self.steps: list[dict[str, Any]] = []
        self.dropped = 0
        self.answers: list[tuple[str, Mapping[str, Any]]] = []
        self.truncated = False
        self.stop: BaseException | None = None

    def latch(self, error: BaseException) -> BaseException:
        if self.stop is None:
            self.stop = error
        return error

    def reraise(self) -> None:
        if self.stop is not None:
            raise self.stop

    def fail(self, message: str, code: str = "adapter-error") -> AdapterError:
        """A broken contract, kept like any stop so the engine raises it even if the adapter catches it."""
        error = AdapterError(message, code)
        self.latch(error)
        return error

    def note(self, entry: dict[str, Any]) -> None:
        if len(self.steps) < MAX_STEPS:
            self.steps.append({"phase": self.phase, **entry})
        else:
            self.dropped += 1

    def call(self, role: str, messages: Any, max_tokens: int | None = None) -> Mapping[str, Any]:
        """One model call; an answer cut at the output limit raises ``output-truncated`` (the judge asks again)."""
        allowed = "executor" if self.phase == "execute" else JUDGE_ROLE
        if role not in self.roles or role != allowed or self.request is None:
            where = "execute usa el rol executor" if self.phase == "execute" else "evaluate usa el rol judge"
            raise self.fail(f"task.model: el rol {role!r} no está disponible aquí ({where}, y cada rol se declara en 'roles' de {DECLARATION}).")
        try:
            prepared = _messages(messages)
        except AdapterError as error:
            self.latch(error)
            raise
        limit = self.roles[role] if max_tokens is None else max_tokens
        try:
            answer = self.request(role, prepared, max_tokens=limit)
        except ProviderError as error:
            if error.code != "output-truncated":
                self.latch(error)
                raise
            self.truncated = True  # the cut answer may have been billed: usage and cost become unknown
            self.note({"kind": "model", "role": role, "maxTokens": limit, "truncated": True})
            raise
        except BaseException as error:
            self.latch(error)
            raise
        self.answers.append((role, answer))
        text = answer.get("text")
        self.note({"kind": "model", "role": role, "maxTokens": limit, "truncated": False, "latencyMs": answer.get("latencyMs"), "usage": answer.get("usage"),
                   "costUsd": answer.get("costUsd"), "reply": _clip(storable(text) if isinstance(text, str) else "", MAX_TRACED)})
        return answer

    def model(self, role: str, messages: Any) -> dict[str, Any]:
        try:
            answer = self.call(role, messages)
        except ProviderError as error:
            if error.code != "output-truncated":
                raise
            return {"text": "", "truncated": True, "usage": None, "costUsd": None, "latencyMs": None}
        text = answer.get("text")
        return {"text": storable(text) if isinstance(text, str) else "", "truncated": False, "usage": answer.get("usage"), "costUsd": answer.get("costUsd"),
                "latencyMs": answer.get("latencyMs")}

    def judge(self, criteria: Any, work: Any, subject: Any) -> dict[str, Any]:
        if JUDGE_ROLE not in self.roles or self.phase != "evaluate":
            raise self.fail(f"task.judge solo existe en evaluate, con el rol judge declarado en 'roles' de {DECLARATION}.")
        try:
            parsed = parse_criteria(criteria, "'criteria'")
            if not isinstance(subject, str) or not subject.strip():
                raise EvaluatorError("'subject' debe decir qué valora el juez.")
            payload = json.loads(json.dumps(work, ensure_ascii=False, allow_nan=False))
            if not isinstance(payload, dict):
                raise EvaluatorError("'work' debe ser un objeto JSON con lo que se entregó.")
        except (EvaluatorError, TypeError, ValueError) as problem:
            raise self.fail(f"task.judge: {_described(problem)}") from None
        try:
            judged = judge_verdict(self.call, judge_messages(subject.strip(), payload, parsed), parsed, self.roles[JUDGE_ROLE])
        except JudgeError as error:
            self.latch(error)
            raise
        return {**judged.record(), "submetrics": judged.verdict.submetrics(), "feedback": judged.verdict.feedback()}

    def run(self, args: Any, timeout: Any, stdin: Any, env: Any) -> dict[str, Any]:
        """A command in the workspace with a time limit, without the credentials of the engine's environment; its output as UTF-8."""
        if not isinstance(args, (list, tuple)) or not args or not all(isinstance(arg, str) and arg and "\0" not in arg for arg in args):
            raise self.fail("task.run: 'args' debe ser una lista no vacía de textos, como [task.python, \"script.py\"].")
        if not _finite(timeout) or not 0 < timeout <= MAX_COMMAND_SECONDS:
            raise self.fail(f"task.run: 'timeout' debe ser un número de segundos mayor que 0 y hasta {MAX_COMMAND_SECONDS}.")
        if (stdin is not None and not isinstance(stdin, str)) or (env is not None and not (isinstance(env, dict) and all(isinstance(key, str) and isinstance(item, str)
                                                                                                                     for key, item in env.items()))):
            raise self.fail("task.run: 'input' debe ser texto y 'env' un objeto de textos.")
        environment = {**child_environment(os.environ, self.hidden), **CHILD_PYTHON, **(env or {})}
        # In a job, a command starts only at a safe point (no cancellation requested, time left) and ends when the attempt's time does;
        # a preview has neither.
        safe_point = getattr(self.request, "check", None)
        if safe_point is not None:
            try:
                safe_point()
            except Stop as stop:
                raise self.latch(stop) from None
        time_left = getattr(self.request, "time_left", None)
        limit = float(timeout) if time_left is None else min(float(timeout), max(0.001, time_left()))
        started = time.monotonic()
        try:
            completed = subprocess.run(list(args), cwd=self.workspace, input=None if stdin is None else storable(stdin).encode("utf-8"), capture_output=True,
                                       timeout=limit, env=environment, check=False)  # nosec B603: the adapter's own command, never a shell
            outcome: dict[str, Any] = {"exitCode": completed.returncode, "stdout": _decoded(completed.stdout), "stderr": _decoded(completed.stderr), "timedOut": False}
        except subprocess.TimeoutExpired as expired:
            if limit < timeout:  # the attempt ran out of time, not the command: a stop of the job, never the candidate's result
                raise self.latch(Stop("time_limit")) from None
            outcome = {"exitCode": None, "stdout": _decoded(expired.stdout), "stderr": _decoded(expired.stderr), "timedOut": True}
        except OSError as failure:
            raise self.latch(InfrastructureError(f"No se pudo iniciar el comando {args[0]!r} ({_described(failure)}); la evaluación se detuvo sin puntuar al candidato.")) from None
        self.note({"kind": "command", "args": [_clip(storable(arg), 200) for arg in args[:20]], "exitCode": outcome["exitCode"], "timedOut": outcome["timedOut"],
                   "seconds": round(time.monotonic() - started, 3), "stdout": _clip(outcome["stdout"], MAX_TRACED), "stderr": _clip(outcome["stderr"], MAX_TRACED)})
        return outcome

    def credential(self, name: Any) -> str:
        if name not in self.credentials:
            raise self.fail(f"task.credential({name!r}): declara el requisito {{\"kind\": \"credential\", \"name\": \"{name}\"}} en {DECLARATION}.")
        value = self.credentials[name]
        if not value:
            raise self.fail(f"Falta la credencial {name}: define la variable de entorno y repite 'gepa adapter check'.", "adapter-requirement-missing")
        return value


class Task:
    """What ``execute(task)`` and ``evaluate(task, observation)`` receive for one case of one candidate.

    ``artifact`` is the candidate artifact (for a Skill, ``{"files": {path: text}}`` with the candidate
    ``SKILL.md``; for a policy, its JSON document), also written at ``artifact_path`` inside
    ``workspace``: the evaluation's own new folder, deleted afterwards. ``input`` is the case's entry,
    all the executor sees of it; ``case`` (the case's id, its input and every field the evaluator reads)
    exists only in ``evaluate``. ``adapter_dir`` is the adapter's frozen folder, for its own resources,
    and ``python`` the engine's interpreter, to run Python the same way on every system.
    """

    def __init__(self, session: _Session, *, artifact: Any, artifact_path: Path, entry: Any, case: dict[str, Any] | None = None) -> None:
        self._session = session
        self._case = case
        self.artifact = artifact
        self.artifact_path = artifact_path
        self.input = entry
        self.workspace = session.workspace
        self.adapter_dir = session.adapter_dir
        self.python = sys.executable

    @property
    def case(self) -> dict[str, Any]:
        if self._case is None:
            raise self._session.fail("task.case solo existe en evaluate: execute recibe task.input, sin lo que solo lee el evaluador.")
        return self._case

    def model(self, role: str, messages: list[dict[str, str]]) -> dict[str, Any]:
        """One call to the model of ``role`` with its declared output limit: ``{"text", "truncated", "usage", "costUsd", "latencyMs"}``.

        An answer cut at that limit comes back with ``truncated`` true and no text: the case's result, not a failure.
        """
        return self._session.model(role, messages)

    def judge(self, criteria: list[dict[str, str]], work: dict[str, Any], subject: str = JUDGED) -> dict[str, Any]:
        """The judge's verdict on ``work`` against ``criteria`` (``[{"name", "description"}]``): score, reasons, summary, submetrics and feedback.

        An invalid or cut verdict is asked again up to 3 times; with none, the evaluation stops without scoring the candidate.
        """
        return self._session.judge(criteria, work, subject)

    def run(self, args: list[str], *, timeout: float, input: str | None = None, env: dict[str, str] | None = None) -> dict[str, Any]:
        """Run a command in the workspace: ``{"exitCode", "stdout", "stderr", "timedOut"}``. ``env`` adds variables (for example a declared credential)."""
        return self._session.run(args, timeout, input, env)

    def credential(self, name: str) -> str:
        """The value of a declared credential; it never reaches the evidence."""
        return self._session.credential(name)

    def infrastructure_error(self, message: str) -> InfrastructureError:
        """An error to raise when the task's environment fails (a service down, a tool that cannot start): it is never the candidate's result."""
        error = InfrastructureError(_clip(storable(str(message)), 500))
        self._session.latch(error)
        return error

    def note(self, message: str) -> None:
        """Add a note to the case's steps."""
        self._session.note({"kind": "note", "message": _clip(storable(str(message)), MAX_TRACED)})


class Environment:
    """What ``check(environment)`` receives to probe what the adapter needs before any model call, such as a browser, an API or a sandbox.

    ``run`` works as in a case, inside ``workspace`` (a new folder, deleted afterwards); ``credential``
    gives a declared credential's value, or ``None`` when it is missing.
    """

    def __init__(self, session: _Session) -> None:
        self._session = session
        self.workspace = session.workspace
        self.adapter_dir = session.adapter_dir
        self.python = sys.executable

    def run(self, args: list[str], *, timeout: float, input: str | None = None, env: dict[str, str] | None = None) -> dict[str, Any]:
        return self._session.run(args, timeout, input, env)

    def credential(self, name: str) -> str | None:
        return self._session.credentials.get(name)


# Requirements ----------------------------------------------------------------------------


def _python_module(name: str) -> bool:
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def _finding(requirement: str, status: str, code: str, message: str, next_step: str = "", resolvable_by: str | None = None) -> dict[str, Any]:
    return {"requirement": requirement, "status": status, "code": code, "message": message, "nextStep": next_step, "resolvableBy": resolvable_by}


def _declared(requirement: Mapping[str, str], environ: Mapping[str, str]) -> dict[str, Any]:
    """How one declared requirement stands, and the next step when it does not hold."""
    kind, name = requirement["kind"], requirement["name"]
    label, why = f"{kind}:{name}", f" (necesario para {requirement['purpose']})"
    install = requirement.get("install")
    if kind == "python":
        if _python_module(name):
            return _finding(label, "ok", "dependency-ok", f"Módulo Python '{name}' disponible.")
        return _finding(label, "error", "dependency-missing", f"Falta el módulo Python '{name}'{why}.",
                        f"Instálalo con: {install or f'{sys.executable} -m pip install {name}'}; luego repite 'gepa adapter check'.", "agent")
    if kind == "command":
        location = shutil.which(name)
        if location:
            return _finding(label, "ok", "dependency-ok", f"Comando '{name}' disponible en {location}.")
        how = f" ({install})" if install else ""
        return _finding(label, "error", "dependency-missing", f"Falta el comando '{name}'{why}.",
                        f"Instálalo{how} y asegúrate de que esté en el PATH del proceso que ejecuta gepa; luego repite 'gepa adapter check'.", "agent")
    if environ.get(name):
        return _finding(label, "ok", "credential-ok", f"Variable de entorno '{name}' definida (su valor no se muestra ni se guarda).")
    return _finding(label, "error", "credential-missing", f"Falta la credencial '{name}'{why}: la variable de entorno no está definida.",
                    f"Pide a la persona que defina la variable de entorno {name} con su valor en el entorno donde se ejecuta gepa (su terminal o la "
                    "configuración del servidor MCP), nunca en la conversación ni en los archivos del experimento; luego repite 'gepa adapter check'.", "person")


def _checked(value: Any) -> list[dict[str, Any]]:
    """The findings ``check(environment)`` returned, or one error finding when they break the contract."""
    def valid(item: Any) -> bool:
        if not isinstance(item, dict) or set(item) - CHECK_KEYS or item.get("status") not in CHECK_STATUSES:
            return False
        texts = ("code", "message", *(("nextStep",) if item["status"] != "ok" else ()))
        if not all(isinstance(item.get(key), str) and item[key].strip() for key in texts):
            return False
        return item.get("resolvableBy") in RESOLVERS if item["status"] == "error" else item.get("resolvableBy") in (None, *RESOLVERS)

    if not isinstance(value, list) or len(value) > 50 or not all(valid(item) for item in value):
        return [_finding("check", "error", "adapter-check-invalid",
                         "check(environment) debe devolver una lista de hallazgos {\"status\": \"ok\" | \"warning\" | \"error\", \"code\", \"message\", \"nextStep\", "
                         "\"resolvableBy\": \"agent\" | \"person\"}; nextStep y resolvableBy son obligatorios en un error.",
                         f"Corrige check() en {CODE} y repite 'gepa adapter check'.", "agent")]
    return [_finding(_clip(storable(str(item.get("requirement", "check"))), 100), item["status"], _clip(storable(item["code"].strip()), 100),
                     _clip(storable(item["message"].strip()), MAX_TEXT), _clip(storable(str(item.get("nextStep", "")).strip()), MAX_TEXT), item.get("resolvableBy"))
            for item in value]


def _credentials(declaration: Mapping[str, Any], environ: Mapping[str, str]) -> dict[str, str | None]:
    return {item["name"]: environ.get(item["name"]) or None for item in declaration["requirements"] if item["kind"] == "credential"}


def _hidden_variables(declaration: Mapping[str, Any], runtime: Runtime) -> frozenset[str]:
    """Variables a command never inherits: the connections' credentials and the adapter's declared ones."""
    return runtime.hidden | {item["name"] for item in declaration["requirements"] if item["kind"] == "credential"}


def diagnose(folder: Path, contents: Folder, declaration: Mapping[str, Any], runtime: Runtime) -> list[dict[str, Any]]:
    """Every requirement of an adapter, then its code and its own ``check``: what holds, and the next step for what does not.

    ``check`` runs only when the declared requirements and the code hold, since it may rely on them.
    """
    findings = [_declared(requirement, runtime.environ) for requirement in declaration["requirements"]]
    try:
        module = _module(folder, contents)
    except (Exception, SystemExit) as error:
        return [*findings, _finding("code", "error", "adapter-code-failed", f"{CODE} no se pudo cargar: {_described(error)}.",
                                    f"Corrige {CODE}; si importa un módulo que falta, instálalo y decláralo en 'requirements'. Luego repite 'gepa adapter check'.", "agent")]
    problem = _contract_problem(module)
    if problem:
        return [*findings, _finding("code", "error", "adapter-contract", problem, f"Corrige {CODE} y repite 'gepa adapter check'.", "agent")]
    findings.append(_finding("code", "ok", "adapter-code-ok", f"{CODE} define execute y evaluate" + (" y check" if hasattr(module, "check") else "") + "."))
    check = getattr(module, "check", None)
    if check is None or any(item["status"] == "error" for item in findings):
        return findings
    try:
        if runtime.workspaces is None:
            raise FileNotFoundError("sin carpeta de espacios aislados")
        workspace = spaces.create(runtime.workspaces).resolve()
    except OSError:
        return [*findings, _finding("workspace", "error", "workspace-unavailable", f"No se pueden crear espacios aislados en {runtime.workspaces}.",
                                    "Comprueba permisos y espacio libre, o elige otra carpeta de datos con: gepa setup data-dir <ruta>.", "agent")]
    credentials = _credentials(declaration, runtime.environ)
    session = _Session(None, roles={}, workspace=workspace, adapter_dir=folder, credentials=credentials, hidden=_hidden_variables(declaration, runtime))
    try:
        value = check(Environment(session))
        if session.stop is not None:
            raise session.stop
    except (Exception, SystemExit) as error:
        failed = _finding("check", "error", "adapter-check-failed", f"check() de {CODE} falló: {_described(error)}.",
                          f"Corrige check() en {CODE}, o resuelve lo que indica el error, y repite 'gepa adapter check'.", "agent")
        return [*findings, *_hidden([failed], _secrets(credentials))]
    finally:
        shutil.rmtree(workspace, ignore_errors=True)
    return [*findings, *_hidden(_checked(value), _secrets(credentials))]


# Results ---------------------------------------------------------------------------------


def _texts(value: Any, change: Callable[[str], str]) -> Any:
    """A JSON value with ``change`` applied to every text in it, keys included."""
    if isinstance(value, str):
        return change(value)
    if isinstance(value, list):
        return [_texts(item, change) for item in value]
    if isinstance(value, dict):
        return {change(key): _texts(item, change) for key, item in value.items()}
    return value


def _invalid(label: str, message: str) -> AdapterError:
    return AdapterError(f"{label}: {message} La evaluación se detuvo sin puntuar al candidato.", "adapter-invalid-result")


def _observation(value: Any, label: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """What ``execute`` returned, checked (a JSON object whose ``output``, if any, is text): all of it, for ``evaluate`` to score, and what
    the evidence keeps of it, which for a very large one is its output, size and hash."""
    if not isinstance(value, dict):
        raise _invalid(label, f"execute debe devolver un objeto JSON (dict) con lo que observó; devolvió {type(value).__name__}.")
    if "output" in value and not isinstance(value["output"], str):
        raise _invalid(label, "'output' de execute debe ser texto: la salida del caso que se muestra en los informes.")
    try:
        text = json.dumps(value, ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError):
        raise _invalid(label, "lo que devolvió execute no es JSON (tiene objetos que no son texto, números, listas u objetos, o un número no finito).") from None
    observation = dict(_texts(json.loads(text), storable))  # every text storable as UTF-8 (a lone UTF-16 surrogate replaced)
    if len(text) <= MAX_OBSERVATION:
        return observation, observation
    output = observation.get("output")
    return observation, {**({"output": _clip(output, MAX_OUTPUT)} if isinstance(output, str) else {}), "omitted": True, "chars": len(text),
                         "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest()}


def _result(value: Any, label: str) -> dict[str, Any]:
    """What ``evaluate`` returned, checked: a score from 0 to 1, a feedback, submetrics; a broken hard requirement scores 0."""
    if not isinstance(value, dict):
        raise _invalid(label, f"evaluate debe devolver un objeto con score y feedback; devolvió {type(value).__name__}.")
    unknown = sorted(set(value) - RESULT_KEYS)
    if unknown:
        raise _invalid(label, f"evaluate devolvió campos que el contrato no admite ({', '.join(unknown)}); admite {', '.join(sorted(RESULT_KEYS))}.")
    score = value.get("score")
    if not _finite(score) or not 0 <= score <= 1:
        raise _invalid(label, f"el score debe ser un número finito de 0 a 1 (más alto es mejor); evaluate devolvió {_clip(repr(score), 80)}.")
    feedback = value.get("feedback")
    if not isinstance(feedback, str) or not feedback.strip():
        raise _invalid(label, "evaluate debe devolver 'feedback': el diagnóstico del caso del que aprende la reflexión.")
    submetrics = value.get("submetrics", {})
    if (not isinstance(submetrics, dict) or len(submetrics) > MAX_SUBMETRICS
            or not all(isinstance(name, str) and 0 < len(name) <= 100 and (isinstance(item, bool) or _finite(item)) for name, item in submetrics.items())):
        raise _invalid(label, f"'submetrics' debe asociar hasta {MAX_SUBMETRICS} nombres con true/false (una comprobación) o un número (un score).")
    if "requirementsMet" in value and not isinstance(value["requirementsMet"], bool):
        raise _invalid(label, "'requirementsMet' debe ser true o false.")
    if value.get("requirementsMet") is False and score != 0:
        raise _invalid(label, "con 'requirementsMet' false el score debe ser 0: un requisito obligatorio incumplido no se compensa.")
    if value.get("taskSuccess") is not None and not isinstance(value["taskSuccess"], bool):
        raise _invalid(label, "'taskSuccess' debe ser true, false o null.")
    if value.get("error") is not None and not isinstance(value["error"], str):
        raise _invalid(label, "'error' debe ser texto o null.")
    return {"score": float(score), "feedback": _clip(storable(feedback.strip()), MAX_FEEDBACK),
            "submetrics": {storable(name): item for name, item in submetrics.items()},
            **{key: value[key] for key in ("requirementsMet", "taskSuccess") if key in value},
            "error": None if value.get("error") is None else _clip(storable(value["error"]), MAX_TEXT)}


def _hidden(value: Any, secrets: Sequence[str]) -> Any:
    """``value`` with every credential value replaced: evidence never holds one."""
    def hide(text: str) -> str:
        for secret in secrets:
            text = text.replace(secret, HIDDEN)
        return text

    return _texts(value, hide)


def _secrets(credentials: Mapping[str, str | None]) -> list[str]:
    """The credential values to hide: those long enough to search for without garbling the texts."""
    return [value for value in credentials.values() if value and len(value) >= MIN_SECRET]


def _redact(error: BaseException, secrets: Sequence[str]) -> None:
    """Replace every credential value in ``error``'s message, so no error, event or stored record quotes one."""
    error.args = tuple(_hidden(arg, secrets) if isinstance(arg, str) else arg for arg in error.args)


def _known(value: Any) -> TypeGuard[int | float]:
    return _finite(value) and value >= 0


def _usage(answers: Sequence[tuple[str, Mapping[str, Any]]], truncated: bool) -> dict[str, Any]:
    """Token totals of a case's model calls; a count one answer did not report, or a cut answer, makes that total unknown."""
    usage: dict[str, Any] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    for _, answer in answers:
        raw = answer.get("usage")
        reported: Mapping[str, Any] = raw if isinstance(raw, dict) else {}
        usage = {key: total + reported[key] if total is not None and _known(reported.get(key)) else None for key, total in usage.items()}
    return dict.fromkeys(usage) if truncated else usage


# The adapter -----------------------------------------------------------------------------


class CustomAdapter:
    """An adapter an agent prepared, loaded from its frozen copy: its ``execute`` and ``evaluate`` run each case on the artifact's surface."""

    executor_role: ClassVar[str] = "executor"
    limit_defaults: ClassVar[dict[str, int]] = {"reflectionMaxTokens": 8192}  # the adapter's own output limits are declared and sealed with the approval
    labels: ClassVar[tuple[str, ...]] = ()  # the adapter's expected values are its own: no per-value coverage or stratified split

    def __init__(self, folder: Path, contents: Folder, declaration: dict[str, Any], module: types.ModuleType, surface: Surface, *, objective: str,
                 runtime: Runtime) -> None:
        self.folder = folder
        self.contents = contents
        self.declaration = declaration
        self.module = module
        self.surface = surface
        self.objective = objective
        self.runtime = runtime
        self.hidden = _hidden_variables(declaration, runtime)
        self.name = f"{declaration['name']} v{declaration['version']}"
        self.label = f"El adaptador {self.name}"

    @classmethod
    def load(cls, folder: Path, kind: Kind, document: Any, *, objective: str, runtime: Runtime) -> CustomAdapter:
        """The adapter of ``folder`` on the original ``document`` of ``kind``. Raises :class:`AdapterError`."""
        contents = read_folder(folder)
        declaration = parse_declaration(contents)
        if declaration["artifact"] != kind.type:
            raise AdapterError(f"El adaptador {declaration['name']} es para artefactos de tipo {declaration['artifact']!r}, no {kind.type!r}: "
                               "elige un adaptador para este artefacto o cambia 'artifact' en su adapter.json.", "invalid-adapter")
        return cls(folder, contents, declaration, _loaded(folder, contents), kind.surface(document), objective=objective, runtime=runtime)

    @property
    def artifact_type(self) -> str:
        return str(self.declaration["artifact"])

    @property
    def roles(self) -> dict[str, str]:
        """Job role -> role configured by setup-gepa: the declared ones, then the engine's reflection."""
        return {**{role: role for role in self.declaration["roles"]}, "reflection": "reflection"}

    @property
    def component(self) -> str:
        return self.surface.component

    @property
    def modules(self) -> tuple[str, ...]:
        """The engine code an approval holds for: this runtime, the judge, the workspaces and the artifact's surface (the adapter's own code has its hash)."""
        return ("custom.py", "declaration.py", "evaluation.py", "spaces.py", Path(inspect.getfile(type(self.surface))).name)

    @property
    def metric_label(self) -> str:
        return f"{self.declaration['evaluation']['primaryMetric']} medio"

    @property
    def preview_max_tokens(self) -> int:
        return int(self.declaration["roles"]["executor"]["maxTokens"])

    @property
    def fields(self) -> list[str]:
        """The case fields the adapter reads: the input, which the executor receives, and the evaluator's."""
        return ["input", *self.declaration["input"]["evaluatorFields"]]

    @property
    def case_fields(self) -> frozenset[str]:
        return frozenset(self.fields)

    @property
    def execution(self) -> dict[str, Any]:
        """How a case runs, recorded in each job's manifest as part of the target the evidence holds for."""
        declared = self.declaration
        return {"agent": f"{declared['name']} v{declared['version']}", "tools": [tool["name"] for tool in declared["tools"]],
                "description": f"{declared['description']} {declared['environment']['description']}",
                "requirements": ["carpeta de datos con permiso de escritura para los espacios aislados", *(f"conexión del rol {role}" for role in declared["roles"]),
                                 *(f"{item['kind']}:{item['name']}" for item in declared["requirements"])],
                "environment": declared["environment"], "adapter": {"name": declared["name"], "version": declared["version"], "sha256": self.contents.sha256}}

    def describe(self) -> dict[str, Any]:
        """What an approval seals: the adapter's identity and code hash, its evaluator, the mutable surface and the fixed task contract."""
        declared = self.declaration
        evaluation = declared["evaluation"]
        return {
            "adapter": {"name": declared["name"], "version": declared["version"], "sha256": self.contents.sha256, "files": self.contents.digests(),
                        "description": declared["description"], "requirements": declared["requirements"]},
            "evaluator": {"name": evaluation["name"], "version": evaluation["version"], "primaryMetric": evaluation["primaryMetric"], "higherIsBetter": True,
                          "description": evaluation["description"], "aggregation": {"rule": evaluation["rule"]}},
            "mutableSurface": self.surface.describe(),
            "fixedContract": {**self.surface.fixed(), "task": {key: declared[key] for key in ("input", "resources", "tools", "environment", "output", "roles")}},
        }

    def original(self) -> Any:
        return self.surface.document(self.surface.seed())

    def _root(self) -> Path:
        if self.runtime.workspaces is None:
            raise WorkspaceError("No hay una carpeta para los espacios aislados.", "workspace-unavailable")
        return self.runtime.workspaces

    def verify_requirements(self) -> None:
        """Before any model call: a workspace can be created, and every declared requirement and the adapter's own ``check`` hold."""
        root = self._root()
        try:
            shutil.rmtree(spaces.create(root))
        except OSError:
            raise WorkspaceError(f"No se pueden crear espacios aislados en {root}. Comprueba permisos y espacio libre, "
                                 "o elige otra carpeta de datos con: gepa setup data-dir <ruta>.", "workspace-unavailable") from None
        findings = diagnose(self.folder, self.contents, self.declaration, self.runtime)
        errors = [item for item in findings if item["status"] == "error"]
        if errors:
            steps = "; ".join(f"{item['message']} Siguiente paso: {item['nextStep']}" for item in errors)
            raise AdapterError(f"{self.label} no tiene lo que necesita: {steps}", "adapter-requirement-missing")

    # Cases ------------------------------------------------------------------------------

    def check_case(self, case: Mapping[str, Any]) -> str | None:
        """What a case lacks for this adapter (a declared field, or what its ``check_case`` reports), or ``None``."""
        declared = self.declaration["input"]
        missing = [field for field in self.fields if field != "input" and field not in declared["optional"] and field not in case]
        if missing:
            return f"falta {', '.join(repr(field) for field in missing)}: {declared['description']}"
        check = getattr(self.module, "check_case", None)
        if check is None:
            return None
        try:
            problem = check(self._case(case))
        except (Exception, SystemExit) as error:
            raise AdapterError(f"check_case del adaptador {self.name} falló con el caso {case.get('id')}: {_described(error)}.", "adapter-error") from None
        if problem is None:
            return None
        if not isinstance(problem, str) or not problem.strip():
            raise AdapterError(f"check_case del adaptador {self.name} debe devolver None o el texto del problema del caso.", "adapter-error")
        return _clip(storable(problem.strip()), MAX_DETAIL)

    def validate_case(self, case: Mapping[str, Any]) -> None:
        problem = self.check_case(case)
        if problem:
            raise ContractError(f"El caso {case.get('id')}: {problem}")

    def _case(self, case: Mapping[str, Any]) -> dict[str, Any]:
        return {"id": case.get("id"), **{field: deepcopy(case[field]) for field in self.fields if field in case}}

    # Candidates -------------------------------------------------------------------------

    def seed(self) -> dict[str, str]:
        return self.surface.seed()

    def artifact(self, candidate: Mapping[str, str]) -> Any:
        return self.surface.artifact(candidate)

    # Execution --------------------------------------------------------------------------

    def _invoke(self, phase: str, session: _Session, function: Callable[..., Any], *arguments: Any) -> Any:
        """Call the adapter's ``execute`` or ``evaluate``; what the engine kept raised during it comes first, then the adapter's own failure."""
        try:
            value = function(*arguments)
        except BaseException as error:
            session.reraise()  # an engine stop, a failed model call or an infrastructure failure is never the adapter's fault
            if isinstance(error, (AdapterError, KeyboardInterrupt)):
                raise
            raise AdapterError(f"{self.label} falló en {phase}: {_described(error)}. La evaluación se detuvo sin puntuar al candidato.", "adapter-error") from None
        session.reraise()
        return value

    def run_case(self, candidate: Mapping[str, str], case: Mapping[str, Any], request: Callable[..., Mapping[str, Any]]) -> dict[str, Any]:
        """Run one case in its own workspace with the adapter's ``execute``, then score it with its ``evaluate``. ``execute`` receives only the case's input."""
        document = self.surface.document(candidate)
        root = self._root()
        lost = "No se pudo preparar, leer o escribir el espacio aislado del caso; la evaluación se detuvo sin puntuar al candidato."
        try:
            workspace = spaces.create(root).resolve()
        except OSError:
            raise WorkspaceError(lost) from None
        credentials = _credentials(self.declaration, self.runtime.environ)
        try:
            return self._run_in(workspace, candidate, case, document, request, credentials)
        except BaseException as error:
            _redact(error, _secrets(credentials))  # the adapter's own message may quote a credential it read
            raise

    def _run_in(self, workspace: Path, candidate: Mapping[str, str], case: Mapping[str, Any], document: Any, request: Callable[..., Mapping[str, Any]],
                credentials: Mapping[str, str | None]) -> dict[str, Any]:
        lost = "No se pudo preparar, leer o escribir el espacio aislado del caso; la evaluación se detuvo sin puntuar al candidato."
        session = _Session(request, roles={role: value["maxTokens"] for role, value in self.declaration["roles"].items()}, workspace=workspace,
                           adapter_dir=self.folder, credentials=credentials, hidden=self.hidden)
        try:
            try:
                artifact_path = self.surface.materialize(candidate, workspace)
                before = spaces.digests(workspace)
            except OSError:
                raise WorkspaceError(lost) from None
            task = Task(session, artifact=deepcopy(document), artifact_path=artifact_path, entry=deepcopy(case["input"]))
            whole, observation = _observation(self._invoke("execute", session, self.module.execute, task), self.label)
            try:
                effects = spaces.effects(workspace, before, MAX_KEPT)
            except OSError:
                raise WorkspaceError(lost) from None
            session.phase = "evaluate"
            judged = Task(session, artifact=deepcopy(document), artifact_path=artifact_path, entry=deepcopy(case["input"]), case=self._case(case))
            result = _result(self._invoke("evaluate", session, self.module.evaluate, judged, deepcopy(whole)), self.label)
        finally:
            shutil.rmtree(workspace, ignore_errors=True)
        # The case's code runs with the person's permissions: a frozen copy it changed would score the next cases with other code.
        if read_folder(self.folder).sha256 != self.contents.sha256:
            raise AdapterError(f"La copia congelada del adaptador en {self.folder} cambió durante la evaluación del caso {case['id']}; la evaluación se detuvo "
                               "sin puntuar al candidato. Prepara y aprueba el dataset de nuevo.", "adapter-changed")
        costs = [answer.get("costUsd") for _, answer in session.answers]
        latencies = [answer.get("latencyMs") for role, answer in session.answers if role == "executor"]
        output = observation.get("output")
        record: dict[str, Any] = {"caseId": case["id"], "split": case["split"], **{field: case[field] for field in self.fields if field in case},
                  "output": _clip(output, MAX_OUTPUT) if isinstance(output, str) else _clip(canonical_json(observation), MAX_TRACED),
                  "score": result["score"], "feedback": result["feedback"], "error": result["error"],
                  **{key: result[key] for key in ("requirementsMet", "taskSuccess") if key in result}, "submetrics": result["submetrics"],
                  "observation": observation, "effects": effects,
                  "steps": [*session.steps, *([{"phase": session.phase, "kind": "omitted", "entries": session.dropped}] if session.dropped else [])],
                  # The candidate's latency is the executor's; usage and cost add every call (the judge's included).
                  "latencyMs": sum(value for value in latencies if _known(value)) if latencies and all(_known(value) for value in latencies) else None,
                  "usage": _usage(session.answers, session.truncated),
                  "costUsd": sum(cost for cost in costs if _known(cost)) if costs and not session.truncated and all(_known(cost) for cost in costs) else None}
        return dict(_hidden(record, _secrets(credentials)))

    # Reflection -------------------------------------------------------------------------

    def reflective_record(self, result: Mapping[str, Any]) -> dict[str, Any]:
        """What the reflection learns from one training case: what the executor received, the score and the evaluator's feedback."""
        return {"caseId": result["caseId"], "input": result["input"], "score": result["score"],
                "feedback": result["feedback"], **({"requirementsMet": result["requirementsMet"]} if "requirementsMet" in result else {}),
                "submetrics": result["submetrics"], "output": _clip(result["output"], MAX_REFLECTED), "error": result["error"]}

    def reflection_messages(self, candidate: Mapping[str, str], records: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
        declared = self.declaration
        evaluation = declared["evaluation"]
        how = (f"Cada caso se ejecuta con el adaptador de experimento {declared['name']} v{declared['version']}: {declared['description']} "
               f"Entorno: {declared['environment']['description']}")
        payload = {"execution": {"adapter": f"{declared['name']} v{declared['version']}", "input": declared["input"]["description"],
                                 "resources": declared["resources"]["description"], "tools": declared["tools"],
                                 "environment": declared["environment"]["description"], "output": declared["output"]["description"]},
                   "evaluation": {"evaluator": evaluation["name"], "description": evaluation["description"], "rule": evaluation["rule"]}}
        return self.surface.reflection_messages(candidate, records, objective=self.objective, how=how,
                                                fixed="los recursos, las herramientas, los casos, el adaptador y el evaluador", payload=payload)

    def parse_proposal(self, text: Any) -> dict[str, str]:
        return self.surface.parse_proposal(text)


# The check the CLI and the MCP tools share -------------------------------------------


def check_report(folder: Path, runtime: Runtime) -> dict[str, Any]:
    """``gepa adapter check``: the declaration, then each requirement, the code and the adapter's ``check``, with the next step for what fails."""
    contents = read_folder(folder)
    declaration = parse_declaration(contents)
    findings = diagnose(folder, contents, declaration, runtime)
    ready = not any(item["status"] == "error" for item in findings)
    if ready:
        step = f"Listo: prepara el dataset con \"adapter\": {{\"path\": \"{folder.as_posix()}\"}} en la solicitud de 'gepa dataset prepare'."
    else:
        step = ("Resuelve cada hallazgo con status error: los de resolvableBy agent, con su nextStep; los de person necesitan un dato o una decisión de la persona. "
                f"Luego repite: gepa adapter check {folder.as_posix()}")
    return {"format": CHECK_FORMAT, "path": str(folder), "ready": ready,
            "adapter": {"name": declaration["name"], "version": declaration["version"], "artifact": declaration["artifact"], "sha256": contents.sha256,
                        "files": contents.digests()},
            "declaration": declaration, "findings": findings, "next": step}
