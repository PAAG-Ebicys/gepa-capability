"""Example adapter ``python-tests``: a Skill guides a model to write a Python function, and each case is scored by running tests on it.

It shows the whole contract an agent adapts for a new task:

- ``check_case(case)`` says what a case lacks (``None`` when it is valid); the engine calls it while preparing.
- ``check(environment)`` probes what the adapter needs before any model call (here, a Python subprocess)
  and returns findings with the next step and who can take it.
- ``execute(task)`` applies the candidate Skill to one case: it receives ``task.input``, never the expected values.
- ``evaluate(task, observation)`` scores what ``execute`` observed with ``task.case``: a score from 0 to 1,
  a diagnostic feedback for GEPA's reflection, submetrics and whether the hard requirements hold.

The candidate's code runs in a subprocess (``harness.py``) that only calls the function: the expected
values stay in this process, which that code cannot reach, so it cannot change its own score.
Messages for the person and for the reflection are in Spanish, like the rest of the engine's output.
"""

import json
import re
from typing import Any

TIMEOUT_SECONDS = 20
FENCE = re.compile(r"```(?:python|py)?[ \t]*\r?\n(.*?)```", re.DOTALL)
TEST_KEYS = {"name", "args", "equals", "required"}


def check_case(case: dict[str, Any]) -> str | None:
    """What a case lacks for this adapter, or None."""
    if not isinstance(case["input"], str) or not case["input"].strip():
        return "'input' debe ser el enunciado de la función."
    expected = case["expected"]
    if not isinstance(expected, dict) or set(expected) != {"function", "tests"}:
        return "'expected' debe ser {\"function\": \"<nombre>\", \"tests\": [...]}."
    if not isinstance(expected["function"], str) or not expected["function"].isidentifier():
        return "'expected.function' debe ser el nombre de la función."
    tests = expected["tests"]
    if not isinstance(tests, list) or not 1 <= len(tests) <= 50:
        return "'expected.tests' debe ser una lista de 1 a 50 pruebas."
    names = set()
    for number, test in enumerate(tests, 1):
        if not isinstance(test, dict) or not {"name", "args", "equals"} <= set(test) <= TEST_KEYS:
            return f"la prueba {number} lleva name, args y equals, y opcionalmente required."
        if not isinstance(test["name"], str) or not test["name"].strip() or test["name"] in names:
            return f"la prueba {number} necesita un 'name' propio."
        if not isinstance(test["args"], list):
            return f"'args' de la prueba {number} debe ser la lista de argumentos."
        if not isinstance(test.get("required", False), bool):
            return f"'required' de la prueba {number} debe ser true o false."
        names.add(test["name"])
    return None


def check(environment: Any) -> list[dict[str, Any]]:
    """Before any model call: a Python subprocess runs in a workspace. Also report how far that subprocess isolates the code."""
    result = environment.run([environment.python, "-c", "print('ok')"], timeout=TIMEOUT_SECONDS)
    if result["timedOut"] or result["exitCode"] != 0 or result["stdout"].strip() != "ok":
        return [{"requirement": "subprocess", "status": "error", "code": "python-subprocess-failed",
                 "message": f"No se pudo ejecutar Python en un subproceso ({result['stderr'].strip()[:200] or 'sin salida'}).",
                 "nextStep": "Comprueba que el Python del motor puede lanzar subprocesos en la carpeta de datos (permisos o antivirus) y repite la comprobación.",
                 "resolvableBy": "agent"}]
    return [{"requirement": "subprocess", "status": "ok", "code": "python-subprocess-ok", "message": "Python se ejecuta en un subproceso con límite de tiempo."},
            {"requirement": "subprocess", "status": "warning", "code": "isolation-limited",
             "message": "El código que escribe el modelo se ejecuta en una carpeta nueva y sin variables de credenciales, pero sin aislar la red ni el resto del sistema de archivos.",
             "nextStep": "Úsalo solo con modelos y casos de confianza; para aislarlo más, ejecuta el código en un contenedor desde evaluate().",
             "resolvableBy": "person"}]


def execute(task: Any) -> dict[str, Any]:
    """One call to the executor with the candidate SKILL.md; its code is written to solution.py in the workspace."""
    reply = task.model("executor", [
        {"role": "system", "content": "Eres un agente que sigue la Skill cargada para escribir código Python.\n\n"
                                      f"Skill cargada (SKILL.md):\n{task.artifact['files']['SKILL.md']}\n\n"
                                      "Devuelve únicamente el contenido completo de solution.py en un bloque ```python```, sin explicaciones."},
        {"role": "user", "content": task.input},
    ])
    if reply["truncated"]:
        return {"output": "", "problem": "La respuesta alcanzó el límite de tokens de salida antes de terminar el código."}
    found = FENCE.search(reply["text"])
    code = found.group(1) if found else reply["text"]
    (task.workspace / "solution.py").write_text(code, encoding="utf-8", newline="")  # the same bytes on every system
    return {"output": code, **({} if found else {"problem": "La respuesta no traía un bloque ```python```: se usó el texto completo como código."})}


def same(got: Any, want: Any) -> bool:
    """The same JSON value and type: 1 is neither true nor 1.0."""
    return json.dumps(got, sort_keys=True, ensure_ascii=False) == json.dumps(want, sort_keys=True, ensure_ascii=False)


def verdict(test: dict[str, Any], result: dict[str, Any]) -> tuple[bool, str]:
    """Whether one test passed, and what went wrong otherwise."""
    if "returned" in result:
        passed = same(result["returned"], test["equals"])
        return passed, "" if passed else f"devolvió {result['returned']!r}; se esperaba {test['equals']!r}"
    if "raised" in result:
        return False, f"lanzó {result['raised']}"
    return False, f"devolvió {result.get('unserializable', 'algo')}, que no es un valor JSON"


def evaluate(task: Any, observation: dict[str, Any]) -> dict[str, Any]:
    """Call the function in one subprocess for all the case's tests and compare here; a required test that fails leaves the case at 0."""
    expected = task.case["expected"]
    tests = expected["tests"]
    calls = {"function": expected["function"], "calls": [test["args"] for test in tests]}
    run = task.run([task.python, str(task.adapter_dir / "harness.py")], timeout=TIMEOUT_SECONDS, input=json.dumps(calls, ensure_ascii=False))
    problem = observation.get("problem")
    lines = run["stdout"].strip().splitlines()
    try:
        report = json.loads(lines[-1]) if lines and not run["timedOut"] else None  # the harness writes its line last, after anything solution.py printed
    except ValueError:
        report = None
    if isinstance(report, dict) and report.get("loaded") and len(report.get("results", [])) == len(tests):
        checked = [verdict(test, result) for test, result in zip(tests, report["results"])]
    else:
        reason = (f"las pruebas no terminaron en {TIMEOUT_SECONDS} s" if run["timedOut"] else
                  f"solution.py no se pudo cargar: {report['error']}" if isinstance(report, dict) and "error" in report else
                  f"el arnés no devolvió resultados ({(run['stderr'].strip() or 'sin salida')[-300:]})")
        checked = [(False, reason) for _ in tests]
        problem = problem or reason[:1].upper() + reason[1:] + "."
    required = {test["name"] for test in tests if test.get("required")}
    broken = [test["name"] for test, (ok, _) in zip(tests, checked) if test["name"] in required and not ok]
    passed = sum(ok for ok, _ in checked)
    failed = [f"{test['name']}: {detail}" for test, (ok, detail) in zip(tests, checked) if not ok]
    feedback = f"Pasan {passed} de {len(tests)} pruebas." + (f" Fallan: {'; '.join(failed)}." if failed else "")
    if broken:
        feedback = f"Fallo: se incumple la prueba obligatoria {', '.join(broken)}; el caso puntúa 0. " + feedback
    if problem:
        feedback = f"{problem} {feedback}"
    return {"score": 0.0 if broken else passed / len(tests), "feedback": feedback, "error": problem,
            "submetrics": {test["name"]: ok for test, (ok, _) in zip(tests, checked)}, "requirementsMet": not broken, "taskSuccess": passed == len(tests)}
