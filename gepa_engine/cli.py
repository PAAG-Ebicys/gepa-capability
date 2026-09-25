"""``gepa`` command line: the common path every host agent can use.

Exit codes: 0 ok, 1 the check (of the setup or of an adapter) found errors, the cases have blocking
problems, the preview failed or the job did not complete, 2 invalid usage, configuration or request.
"""

from __future__ import annotations

import argparse
import contextlib
import getpass
import json
import os
import sys
from functools import partial
from pathlib import Path
from typing import Any, Callable, NoReturn, Sequence, TextIO

from . import __version__
from .artifacts import KINDS, load_json
from .config import (
    DEFAULT_FOLDERS,
    ConfigError,
    default_home,
    default_settings,
    load_settings,
    make_connection,
    make_dependency,
    open_secret_store,
    public_view,
    save_settings,
    with_connection,
    with_data_dir,
    with_dependency,
    with_role,
    without_connection,
)
from .datasets import provenance_text
from .declaration import DEFAULT_EXAMPLE, examples
from .doctor import Probes, real_probes, render_report, run_doctor
from .encoding import utf8
from .folders import designate
from .install import HOSTS, install
from .jobs import PREVIEW_SAMPLE, Gateway, JobError, JobService, Launcher, error_payload
from .lifecycle import RETRY_KINDS
from .providers import ProviderError
from .review import ROLE_NAMES, template_label
from .templates import examples as template_examples


class _Parser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:  # argparse would call sys.exit(2); raise instead so main() owns the exit code
        raise _Usage(message)


class _Usage(Exception):
    pass


def build_parser() -> argparse.ArgumentParser:
    parser = _Parser(prog="gepa", description="Motor local GEPA: configuración, diagnóstico e integración con agentes anfitriones.")
    parser.add_argument("--home", help="Carpeta de configuración (por defecto GEPA_HOME o .gepa en la carpeta actual).")
    parser.add_argument("--version", action="version", version=f"gepa-capability {__version__}")
    commands = parser.add_subparsers(dest="command", metavar="comando")

    setup = commands.add_parser("setup", help="Instalar, configurar y comprobar GEPA.")
    setup_commands = setup.add_subparsers(dest="setup_command", metavar="acción")

    init = setup_commands.add_parser("init", help="Crear la configuración inicial.")
    init.add_argument("--data-dir", help="Carpeta donde se guardarán trabajos y resultados.")

    show = setup_commands.add_parser("show", help="Mostrar la configuración actual (sin credenciales).")
    show.add_argument("--json", action="store_true")

    check = setup_commands.add_parser("check", help="Diagnosticar motor, dependencias, conexiones, credenciales y roles.")
    check.add_argument("--json", action="store_true")

    data_dir = setup_commands.add_parser("data-dir", help="Cambiar la carpeta de datos.")
    data_dir.add_argument("path")

    folders = setup_commands.add_parser("folders", help="Designar las carpetas del proyecto para las plantillas y los adaptadores propios (por defecto "
                                                        f"{DEFAULT_FOLDERS['templates']}/ y {DEFAULT_FOLDERS['adapters']}/), crearlas y añadir al .gitignore "
                                                        "las reglas que dejan fuera de git la carpeta del motor, las plantillas y los adaptadores, pero no "
                                                        "las copias para compartir.")
    folders.add_argument("--templates", default=None, help="Carpeta de plantillas: relativa a la raíz del proyecto, o absoluta.")
    folders.add_argument("--adapters", default=None, help="Carpeta de adaptadores propios: relativa a la raíz del proyecto, o absoluta.")
    folders.add_argument("--json", action="store_true")

    role = setup_commands.add_parser("role", help="Asignar una conexión a un rol (executor, reflection, judge).")
    role.add_argument("role")
    role.add_argument("connection_id")

    connection = setup_commands.add_parser("connection", help="Gestionar conexiones a modelos.")
    connection_commands = connection.add_subparsers(dest="connection_command", metavar="acción")
    add = connection_commands.add_parser("add", help="Añadir o reemplazar una conexión.")
    add.add_argument("--id", required=True)
    add.add_argument("--provider", required=True, choices=("local", "openrouter"))
    add.add_argument("--model", required=True)
    add.add_argument("--url", default="", help="URL base terminada en /v1 (obligatoria para local).")
    add.add_argument("--name", default="")
    add.add_argument("--api-key-env", default=None, help="Variable de entorno que contiene la API key; la clave nunca se guarda en la configuración.")
    connection_commands.add_parser("list", help="Listar conexiones.")
    remove = connection_commands.add_parser("remove", help="Eliminar una conexión.")
    remove.add_argument("connection_id")
    set_key = connection_commands.add_parser("set-key", help="Guardar la API key cifrada (se lee de la entrada estándar, nunca de los argumentos).")
    set_key.add_argument("connection_id")

    dependency = setup_commands.add_parser("dependency", help="Declarar dependencias de ejecución que el diagnóstico debe comprobar.")
    dependency_commands = dependency.add_subparsers(dest="dependency_command", metavar="acción")
    dep_add = dependency_commands.add_parser("add")
    dep_add.add_argument("--kind", required=True, choices=("python", "command"))
    dep_add.add_argument("--name", required=True)
    dep_add.add_argument("--purpose", default="")

    host_install = setup_commands.add_parser("install", help="Instalar las skills GEPA en un agente anfitrión y registrar MCP si lo admite.")
    host_install.add_argument("--host", required=True, choices=sorted(HOSTS))
    host_install.add_argument("--scope", default="project", choices=("project", "user"))
    host_install.add_argument("--target", default=None, help="Raíz del proyecto (por defecto, la carpeta actual).")
    host_install.add_argument("--no-mcp", action="store_true")
    host_install.add_argument("--json", action="store_true")

    dataset = commands.add_parser("dataset", help="Preparar, previsualizar y aprobar versiones selladas de casos.")
    dataset_commands = dataset.add_subparsers(dest="dataset_command", metavar="acción")
    prepare = dataset_commands.add_parser("prepare", help="Importar y validar casos para una política JEV o una Skill; crea un borrador si nada lo impide.")
    prepare.add_argument("request", help="Solicitud JSON: artifact, objective, inputs, split opcional y, para una Skill, executor y evaluator opcionales, "
                                         "adapter con la carpeta de un adaptador propio, o template con una plantilla que da los tres; sus rutas relativas "
                                         "se resuelven desde su carpeta.")
    prepare.add_argument("--json", action="store_true")
    preview = dataset_commands.add_parser("preview", help="Ejecutar el original (política o Skill) sobre algunos casos de train y val del borrador.")
    preview.add_argument("draft_id")
    preview.add_argument("--sample", type=int, default=None, help=f"Casos a ejecutar (por defecto {PREVIEW_SAMPLE}; en JEV, siempre uno por opción).")
    preview.add_argument("--decider", "--executor", dest="decider", default=None, help="Conexión del modelo que ejecuta el original: el decisor JEV o el ejecutor de la Skill (por defecto, el rol executor).")
    preview.add_argument("--judge", default=None, help="Conexión del juez con el evaluador rubric-judge (por defecto, el rol judge).")
    preview.add_argument("--json", action="store_true")
    approve = dataset_commands.add_parser("approve", help="Sellar un borrador previsualizado como versión aprobada e inmutable.")
    approve.add_argument("draft_id")
    approve.add_argument("--reviewed-all", action="store_true", help="La persona revisó el dataset completo (queda registrado en la aprobación).")
    approve.add_argument("--json", action="store_true")
    dataset_show = dataset_commands.add_parser("show", help="Mostrar un borrador o una versión aprobada.")
    dataset_show.add_argument("dataset_id")
    dataset_show.add_argument("--cases", action="store_true", help="Incluir todos los casos, para revisarlos completos.")
    dataset_show.add_argument("--json", action="store_true")

    adapter = commands.add_parser("adapter", help="Preparar y comprobar adaptadores de experimento para tareas que los incluidos no cubren.")
    adapter_commands = adapter.add_subparsers(dest="adapter_command", metavar="acción")
    adapter_init = adapter_commands.add_parser("init", help="Copiar un adaptador de ejemplo en una carpeta nueva, para adaptarlo a la tarea.")
    adapter_init.add_argument("folder", help="Carpeta nueva o vacía.")
    adapter_init.add_argument("--example", default=DEFAULT_EXAMPLE, help=f"Ejemplo a copiar: {', '.join(examples())} (por defecto {DEFAULT_EXAMPLE}).")
    adapter_init.add_argument("--json", action="store_true")
    adapter_check = adapter_commands.add_parser("check", help="Validar un adaptador y diagnosticar sus requisitos (módulos, comandos, credenciales y su propia "
                                                              "comprobación) sin llamar a modelos.")
    adapter_check.add_argument("folder", help="Carpeta con adapter.json y adapter.py.")
    adapter_check.add_argument("--json", action="store_true")

    template = commands.add_parser("template", help="Guardar, comprobar, listar y copiar de un ejemplo plantillas de experimento: carpetas del proyecto para "
                                                    "repetir una clase de prueba. Confiar en el programa de una plantilla traída de fuera.")
    template_commands = template.add_subparsers(dest="template_command", metavar="acción")
    template_save = template_commands.add_parser("save", help="Guardar la clase de tarea de un dataset aprobado como carpeta de plantilla nueva (versión 1): "
                                                              "adaptador, ejecutor, evaluador, guía de casos y presentación, sin casos ni resultados. "
                                                              "Nunca sobrescribe una carpeta.")
    template_save.add_argument("request", help="Solicitud JSON: from (el ds-… aprobado), name y, opcionales, description, cases.guide, presentation y path "
                                               "(la carpeta nueva, relativa a la de la solicitud; por defecto, una con su nombre en la carpeta de plantillas).")
    template_save.add_argument("--json", action="store_true")
    template_check = template_commands.add_parser("check", help="Comprobar una carpeta de plantilla sin el original y sin ejecutar su adaptador: lo que el motor "
                                                                "deduce, su SHA-256 y cómo aplicarla, o sus problemas campo por campo.")
    template_check.add_argument("folder", help="Carpeta de la plantilla, con template.json.")
    template_check.add_argument("--json", action="store_true")
    template_list = template_commands.add_parser("list", help="Listar las plantillas de la carpeta de plantillas del proyecto (sin las copias para compartir).")
    template_list.add_argument("--artifact", default=None, choices=sorted(KINDS), help="Solo las plantillas de este tipo de artefacto.")
    template_list.add_argument("--json", action="store_true")
    template_init = template_commands.add_parser("init", help="Copiar una plantilla de ejemplo de GEPA en una carpeta nueva, para adaptarla: usa un adaptador "
                                                              "incluido y ningún programa propio.")
    template_init.add_argument("folder", nargs="?", default=None, help="Carpeta nueva (por defecto, una con el nombre del ejemplo en la carpeta de plantillas).")
    template_init.add_argument("--example", required=True, choices=template_examples(), help="Plantilla de ejemplo a copiar.")
    template_init.add_argument("--json", action="store_true")
    template_trust = template_commands.add_parser("trust", help="Confiar en el programa del adaptador propio de una plantilla traída de otro proyecto. Lo "
                                                                "ejecuta la persona en su terminal, nunca el agente: no hay tool MCP que lo haga.")
    template_trust.add_argument("folder", help="Carpeta de la plantilla, con template.json y adapter/.")
    template_trust.add_argument("--sha256", default=None, help="SHA-256 del programa que se revisó: si el programa cambió, no se registra nada.")
    template_trust.add_argument("--json", action="store_true")

    job = commands.add_parser("job", help="Iniciar, consultar y exportar trabajos de optimización.")
    job_commands = job.add_subparsers(dest="job_command", metavar="acción")
    job_start = job_commands.add_parser("start", help="Congelar un trabajo desde su especificación JSON y ejecutarlo.")
    job_start.add_argument("spec")
    job_start.add_argument("--detach", action="store_true", help="Ejecutar en un proceso aparte y volver de inmediato.")
    job_start.add_argument("--json", action="store_true")
    job_run = job_commands.add_parser("run", help="Ejecutar un trabajo en cola.")
    job_run.add_argument("job_id")
    job_run.add_argument("--json", action="store_true")
    job_show = job_commands.add_parser("show", help="Consultar estado, candidatos y evidencia de un trabajo.")
    job_show.add_argument("job_id")
    job_show.add_argument("--cases", action="store_true", help="Incluir la evidencia por caso.")
    job_show.add_argument("--json", action="store_true")
    job_list = job_commands.add_parser("list", help="Listar trabajos.")
    job_list.add_argument("--json", action="store_true")
    job_cancel = job_commands.add_parser("cancel", help="Cancelar un trabajo desde cualquier sesión: en cola, al momento; en curso, antes de su siguiente "
                                                        "llamada. Conserva candidatos y evidencia.")
    job_cancel.add_argument("job_id")
    job_cancel.add_argument("--json", action="store_true")
    job_retry = job_commands.add_parser("retry", help="Reintentar un trabajo detenido con una de las recuperaciones que ofrece: con la selección "
                                                      "congelada, solo los casos pendientes de la prueba reservada; tras una búsqueda cortada, "
                                                      "continuarla desde el último estado guardado de GEPA o cerrar con los candidatos ya validados.")
    job_retry.add_argument("job_id")
    job_retry.add_argument("--kind", choices=RETRY_KINDS, default=None,
                           help="La recuperación elegida, una de las que muestra 'gepa job show': continue-search (continuar la búsqueda), "
                                "final-retry (cerrar con lo validado o terminar la prueba reservada) o run (ejecutar completo). "
                                "Obligatoria cuando el trabajo ofrece dos.")
    job_retry.add_argument("--detach", action="store_true", help="Ejecutar en un proceso aparte y volver de inmediato.")
    job_retry.add_argument("--json", action="store_true")
    job_review = job_commands.add_parser("review", help="Revisar la evidencia guardada: original frente a hasta cinco candidatos, por caso, sin llamar a modelos.")
    job_review.add_argument("job_id")
    job_review.add_argument("--candidate", action="append", dest="candidates", default=None, metavar="ID",
                            help="Candidato a comparar con el original (repetible, hasta 5; por defecto, los finalistas).")
    job_review.add_argument("--case", default=None, metavar="CASO", help="Mostrar toda la evidencia guardada de un caso.")
    job_review.add_argument("--cases", action="store_true", help="Incluir una fila por caso de validación y de prueba reservada.")
    job_review.add_argument("--json", action="store_true")
    job_export = job_commands.add_parser("export", help="Exportar un finalista (policy.json o la carpeta skill/) con su manifiesto y evidencia a una carpeta nueva.")
    job_export.add_argument("job_id")
    job_export.add_argument("--out", required=True, help="Carpeta nueva o vacía; nunca se escribe sobre el original.")
    job_export.add_argument("--candidate", default=None, help="Id del finalista (por defecto, el seleccionado por validación).")
    job_export.add_argument("--json", action="store_true")

    commands.add_parser("mcp", help="Servir las mismas operaciones como servidor MCP por stdio.")
    return parser


def _emit(stdout: TextIO, payload: dict[str, Any], as_json: bool, human: str | None = None) -> None:
    stdout.write((json.dumps(payload, ensure_ascii=False, indent=2) if as_json else (human or json.dumps(payload, ensure_ascii=False, indent=2))) + "\n")


def _score_text(score: dict[str, Any] | None) -> str:
    return "—" if not score else f"{score['correct']:g}/{score['total']} ({score['score']:.1%})"


def _measure_text(measurement: dict[str, Any] | None) -> str:
    if measurement and measurement["status"] != "complete":
        return f"{measurement['status']} ({measurement['total']} casos)"
    return _score_text(measurement)


def _text(value: Any, limit: int = 200) -> str:
    """One-line text of a case field or output: line breaks shown as \\n, so each case keeps its own line."""
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    text = text.replace("\r", "\\r").replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + "…"


SESSION_NAMES = {"finished": "terminó", "turn-limit": "límite de turnos", "output-truncated": "respuesta truncada"}  # how a Skill session ended
EFFECT_NAMES = {"created": "creado", "modified": "modificado"}
STATUS_NAMES = {"ok": "ok", "warning": "aviso", "error": "ERROR"}  # a finding of an adapter check
RESOLVER_NAMES = {"agent": "lo resuelve el agente", "person": "necesita a la persona"}


def _metric_text(value: Any) -> str:
    """A submetric of one case: a check as sí/no, a score (a judge's, a criterion's) as its number."""
    return ("sí" if value else "no") if isinstance(value, bool) else f"{value:.2f}" if isinstance(value, (int, float)) else "—"


def _submetrics_text(submetrics: dict[str, Any] | None) -> str:
    return ", ".join(f"{name} {_metric_text(value)}" for name, value in (submetrics or {}).items())


def _requirements_text(record: dict[str, Any]) -> str:
    """Whether a case met its hard requirements; empty for records without them (a JEV decision, a job before they existed)."""
    if record.get("requirementsMet") is None:
        return ""
    return " · requisitos obligatorios: " + ("cumplidos" if record["requirementsMet"] else "INCUMPLIDOS (el caso puntúa 0)")


def _produced(record: dict[str, Any]) -> str:
    """The files a case created or modified in its isolated workspace."""
    return ", ".join(f"{item['path']} {EFFECT_NAMES.get(item['status'], item['status'])}" for item in record["effects"]["files"]) or "ninguno"


def _session_text(record: dict[str, Any]) -> str:
    """What a Skill session did in one case: how it ended, its turns and the files it produced."""
    session = record["session"]
    return f"sesión {SESSION_NAMES.get(session['status'], session['status'])} en {len(session['turns'])} turnos · archivos: {_produced(record)}"


def _observation_text(record: dict[str, Any]) -> str:
    """What a case of an agent's adapter produced: its files, and the model calls and commands among its steps."""
    kinds = [step["kind"] for step in record["steps"]]
    return f"archivos: {_produced(record)} · pasos: {kinds.count('model')} llamadas a modelos, {kinds.count('command')} comandos"


def _outcome_text(record: dict[str, Any]) -> str:
    """How one case went: a Skill session, what an agent's adapter observed, or a JEV decision."""
    if "session" in record:
        return _session_text(record)
    if "observation" in record:
        return _observation_text(record)
    return f"decisión {record['decision'] or '—'}"


def _task_lines(view: dict[str, Any]) -> list[str]:
    """The task an agent's adapter declares, as the approval seals it: what a person checks before approving."""
    task, adapter = view["fixedContract"]["task"], view["adapter"]
    fields, environment = task["input"], task["environment"]
    tools = "; ".join(f"{tool['name']} — {tool['description']}" for tool in task["tools"]) or "ninguna"
    requirements = ", ".join(f"{item['kind']}:{item['name']}" for item in adapter["requirements"]) or "ninguno declarado"
    models = ", ".join(f"{role} hasta {value['maxTokens']} tokens por llamada" for role, value in task["roles"].items())
    return [f"Adaptador propio: {adapter['description']} (sha256 {adapter['sha256'][:12]}; carpeta {view.get('adapterSource', '—')})",
            f"  Entrada: {fields['description']} (el ejecutor ve 'input'; solo el evaluador: {', '.join(fields['evaluatorFields']) or 'nada'})",
            f"  Recursos: {task['resources']['description']}",
            f"  Herramientas: {tools}",
            f"  Entorno: {environment['description']} · red: {'sí' if environment['network'] else 'no'} · ejecución de código: "
            f"{'sí' if environment['codeExecution'] else 'no'} · aislamiento: {environment['isolation']}",
            f"  Salida observable: {task['output']['description']}",
            f"  Modelos: {models} · requisitos: {requirements}"]


def render_adapter_check(report: dict[str, Any]) -> str:
    """Human summary of what ``gepa adapter check --json`` prints."""
    adapter = report["adapter"]
    lines = [f"Adaptador {adapter['name']} v{adapter['version']} para artefactos {adapter['artifact']} (sha256 {adapter['sha256'][:12]}): "
             f"{'listo' if report['ready'] else 'no está listo'}", f"Carpeta: {report['path']}"]
    for item in report["findings"]:
        step = f" → {item['nextStep']}" if item["nextStep"] else ""
        who = f" ({RESOLVER_NAMES[item['resolvableBy']]})" if item.get("resolvableBy") else ""
        lines.append(f"  - [{STATUS_NAMES[item['status']]} {item['code']}] {item['requirement']}: {item['message']}{step}{who}")
    lines.append(f"Siguiente paso: {report['next']}")
    return "\n".join(lines)


def render_preview(preview: dict[str, Any]) -> str:
    """Human summary of what ``gepa dataset preview --json`` prints."""
    cost = "desconocido" if preview["costUsd"] is None else f"{preview['costUsd']:.4f} USD"
    role = preview.get("role", "decider")
    judge = (preview.get("models") or {}).get("judge")
    lines = [f"Previsualización del original en {preview['draftId']}: {preview['status']} · {_score_text(preview['score'])} · "
             f"{ROLE_NAMES.get(role, role)} {preview['model']['model']}" + (f" · juez {judge['model']}" if judge else "")
             + f" · llamadas {preview['counts']['modelCalls']} · coste {cost}"]
    for case in preview["cases"]:
        outcome = f" · {_outcome_text(case)}" if "session" in case or "observation" in case else ""
        rubric = f" · rúbrica propia {_text(case['rubric'], 200)}" if case.get("rubric") else ""
        # An adapter an agent prepared may give its cases no 'expected': the evaluator reads other fields.
        lines.append(f"  - {case['caseId']} [{case['split']}] entrada {_text(case['input'])} · esperado {_text(case.get('expected', '—'), 120)}{rubric} · "
                     f"salida {_text(case['output'], 120)}{outcome} · score {case['score']:g}{_requirements_text(case)} · "
                     f"submétricas: {_submetrics_text(case['submetrics'])} · {case['feedback']}")
    if preview["error"]:
        lines.append(f"Error [{preview['error']['code']}]: {preview['error']['message']}")
    lines.append(f"Siguiente paso: {preview['next']}")
    return "\n".join(lines)


def render_dataset(view: dict[str, Any]) -> str:
    """Human summary of a draft or an approved version: the same view ``--json`` prints."""
    if view["kind"] == "dataset":
        title = f"Dataset aprobado {view['datasetId']} (sha256 {view['sha256'][:12]}) desde el borrador {view['draftId']}"
    else:
        title = f"Borrador {view['draftId']}" if view["ready"] else "Los casos no se pueden aprobar todavía"
    split = view["split"]
    rule = "explícita" if split["mode"] == "explicit" else f"automática con semilla {split['seed']}"
    sizes = view["counts"]
    artifact = view["artifact"]
    if "template" in view:
        template = view["template"]
        title += f" · desde la plantilla {template_label(template)}"
    if artifact["type"] == "skill":
        what = (f"Skill {artifact['name'] or '(sin name en el frontmatter)'} (sha256 {artifact['sha256'][:12]}; {len(artifact['files'])} archivos; "
                f"solo cambia {view['mutableSurface']['component']})")
    else:
        what = f"Política {artifact['question']} (sha256 {artifact['sha256'][:12]})"
    evaluator = view["evaluator"]
    lines = [title, f"Objetivo: {view['objective'] or '(sin indicar)'}",
             f"{what} · evaluador {evaluator['name']} v{evaluator['version']} · adaptador {view['adapter']['name']} v{view['adapter']['version']}",
             f"Criterio: {evaluator['description']}"]
    if "task" in view["fixedContract"]:  # an adapter an agent prepared declares its task
        lines.extend(_task_lines(view))
    if "aggregation" in evaluator:  # evaluators sealed before the rule was recorded do not have it
        lines.append(f"Regla de agregación por caso: {evaluator['aggregation']['rule']}")
    if "judge" in evaluator:
        lines.append(f"Juez: rol {evaluator['judge']['role']}, instrucciones {evaluator['judge']['prompt']}, hasta {evaluator['judge']['maxTokens']} tokens por veredicto")
        lines.append("Rúbrica común:" if evaluator["rubric"] else "Rúbrica común: (ninguna; cada caso trae la suya)")
        lines.extend(f"  - {criterion['name']}: {criterion['description']}" for criterion in evaluator["rubric"])
    lines.append(f"Particiones (división {rule}): train {sizes['train']} · val {sizes['val']} · test {sizes['test']}")
    if artifact["type"] == "skill":
        if "executor" in view:  # the built-in isolated session
            executor = view["executor"]
            lines.append(f"Ejecutor: {executor['name']} v{executor['version']}, hasta {executor['maxTurns']} turnos de {executor['maxTokensPerTurn']} tokens, "
                         f"acciones {', '.join(executor['actions'])}; sin red ni ejecución de código")
        lines.append(f"Archivos de la Skill: {', '.join(artifact['files'])}")
    elif view["coverage"]["expected"]:
        lines.append("Cobertura por opción (train/val/test):")
        for option, counts in view["coverage"]["expected"].items():
            lines.append(f"  - {option}: {counts['train']}/{counts['val']}/{counts['test']} — {view['criteria'].get(option, '')}")
    lines.append("Procedencia:")
    lines.extend(f"  - {item['source']}: {item['cases']} casos" for item in view["coverage"]["sources"])
    errors = [issue for issue in view["issues"] if issue["severity"] == "error"]
    if view["issues"]:
        lines.append(f"Problemas: {len(errors)} errores, {len(view['issues']) - len(errors)} avisos")
        lines.extend(f"  - [{issue['severity']} {issue['code']}] {issue['message']}" for issue in view["issues"])
    preview = view["preview"]
    lines.append("Previsualización del original: " + ("pendiente" if preview is None else f"{preview['status']} · {_score_text(preview['score'])}"))
    lines.append(view["review"]["recommendation"])
    if view["review"]["fullDataset"]:
        lines.append(f"Dataset completo: {view['review']['fullDataset']['cli']}")
    lines.append(f"Siguiente paso: {view['next']}")
    if "cases" in view:
        lines.append(f"Casos ({len(view['cases'])}):")
        for case in view["cases"]:
            rubric = f" · rúbrica propia {_text(case['rubric'], 10_000)}" if "rubric" in case else ""
            lines.append(f"  - {case['id']} [{case.get('split', '—')}] esperado {_text(case.get('expected', '—'), 10_000)}{rubric} · "
                         f"{_text(case.get('input'), 10_000)} · {provenance_text(case)}")
    return "\n".join(lines)


def _calls_text(calls: dict[str, int]) -> str:
    return ", ".join(f"{ROLE_NAMES.get(role, role)} {number}" for role, number in calls.items())


def _spent_text(spent: dict[str, Any]) -> str:
    tokens = spent["usage"]["total_tokens"]
    return (f"llamadas: {_calls_text(spent['modelCalls'])} · tokens {tokens if tokens is not None else 'desconocidos'} · "
            f"{_cost_text(spent['costUsd'])} · {spent['seconds']:g} s")


def _search_text(spent: dict[str, Any]) -> str:
    """The search against its frozen budget."""
    return f"Búsqueda: evaluaciones {spent['evaluations']}/{spent['maxEvaluations']} · {_spent_text(spent)}"


def _final_text(spent: dict[str, Any]) -> str:
    """The reserved test: results in of those it needs, then what every attempt spent, failed evaluations included."""
    attempts = spent["attempts"]
    return (f"Prueba reservada: casos resueltos {spent['resolved']}/{spent['required']} · evaluaciones {spent['evaluations']} en {attempts} "
            f"{'intento' if attempts == 1 else 'intentos'} (fallidas incluidas) · {_spent_text(spent)}")


def render_job(view: dict[str, Any]) -> str:
    """Human summary of the same view ``--json`` prints."""
    manifest = view["manifest"]
    counts, models, sizes = view["counts"], manifest["models"], manifest["dataset"]["counts"]
    phase = f" (fase {view['phase']})" if view["phase"] else ""
    attempts = len(view["attempts"])
    roles = " · ".join(f"{ROLE_NAMES.get(role, role)}: {model['model']}" for role, model in models.items())
    lines = [
        f"Trabajo {view['jobId']} «{view['name']}»: {view['status']}{phase}" + (f" · intento {attempts}" if attempts > 1 else ""),
        f"Manifiesto {view['manifestSha256'][:12]} · dataset {manifest['dataset']['id']} (train {sizes['train']} · val {sizes['val']} · test {sizes['test']})",
        roles[:1].upper() + roles[1:],
    ]
    consumption = view["consumption"]
    if consumption["search"]:
        lines.append(_search_text(consumption["search"]))
    if consumption["final"]:
        lines.append(_final_text(consumption["final"]))
    if consumption["countsLowerBound"]:
        lines.append("Las llamadas y las evaluaciones de la prueba reservada son un mínimo: un intento terminó sin registrar lo último que hizo. "
                     "Las evaluaciones de búsqueda constan todas.")
    if counts:
        lines.append(f"Total del trabajo ({attempts} {'intento' if attempts == 1 else 'intentos'}): llamadas: {_calls_text(counts['modelCalls'])} · "
                     f"{_cost_text(view['costUsd'])} · límite de tiempo por intento: {consumption['timeLimitMinutes']:g} min")
    if view["candidates"]:
        lines.append("Candidatos (validación · prueba reservada):")
        for candidate in view["candidates"]:
            flags = [name for name, on in (("finalista", candidate["finalist"]), ("seleccionado", candidate["selected"])) if on]
            suffix = f" [{', '.join(flags)}]" if flags else ""
            lines.append(f"  - {candidate['label']} {candidate['id']}: {_measure_text(candidate['validation'])} · {_measure_text(candidate['test'])}{suffix}")
    if view["error"]:
        lines.append(f"Error [{view['error']['code']}]: {view['error']['message']}")
    selection = view["selection"]
    if view["status"] == "completed" and selection:
        chosen = next(candidate["label"] for candidate in view["candidates"] if candidate["id"] == selection["selectedId"])
        # The recommendation also weighs the reserved test and earlier jobs: only the review states it.
        lines.append(f"Seleccionado por validación antes de la prueba reservada: {chosen}; la recomendación y la evidencia por caso están en la revisión.")
    lines.append(f"Siguiente paso: {view['next']}")
    return "\n".join(lines)


PHASE_NAMES = {"search": "búsqueda", "validation": "validación", "test": "prueba reservada"}


def _review_measure(measure: dict[str, Any]) -> str:
    if measure["status"] == "complete":
        return f"{measure['correct']:g}/{measure['total']} ({measure['score']:.1%})"
    if measure["status"] == "partial":
        return f"parcial: {measure['evaluated']} de {measure['total']} casos, sin porcentaje"
    return "no evaluado"


def _id_list(case_ids: list[str], limit: int = 5) -> str:
    return ", ".join(case_ids[:limit]) + (f" y {len(case_ids) - limit} más" if len(case_ids) > limit else "")


def _cost_text(cost: Any) -> str:
    return "coste desconocido" if cost is None else f"coste {cost:.4f} USD"


def _evaluation_text(item: dict[str, Any]) -> str:
    """One stored evaluation of a case: what the candidate produced, how it scored and what it cost."""
    trace = item["trace"]
    tokens = (trace["usage"] or {}).get("total_tokens")
    score = f"score {item['score']:g}" if isinstance(item["score"], (int, float)) else "score —"
    checks = _submetrics_text(item["submetrics"])
    outcome = _outcome_text(item)
    parts = [f"{item['label']} {item['candidateId']} ({PHASE_NAMES.get(item['phase'], item['phase'])})", score + _requirements_text(item), outcome,
             f"salida {_text(item['output'], 200)}", *([checks] if checks else []), str(item["feedback"])]
    if item["error"]:
        parts.append(f"error: {item['error']}")
    parts += [f"{trace['latencyMs']} ms" if trace["latencyMs"] is not None else "latencia —",
              f"tokens {tokens if tokens is not None else 'desconocidos'}", _cost_text(trace["costUsd"])]
    return "  - " + " · ".join(parts)


def _mean_text(value: Any) -> str:
    return "—" if value is None else f"{value:.2f}"


def _row_outcome(item: dict[str, Any]) -> str:
    """The short outcome of one case row: how a Skill session ended, the JEV decision, or whether an agent's adapter reported an error."""
    if "session" in item:
        return SESSION_NAMES.get(item["session"], item["session"])
    if "decision" not in item:  # an agent's adapter makes no decision
        return "con error" if item.get("error") else "evaluado"
    return item["decision"] or "salida inválida"


def render_review(report: dict[str, Any]) -> str:
    """Human report of what ``gepa job review --json`` prints; every figure comes from that JSON."""
    counts, completeness, metric = report["counts"], report["completeness"], report["primaryMetric"]
    cases, evaluations, internal = counts["cases"], counts["evaluations"], counts["internalChecks"]
    calls = ", ".join(f"{ROLE_NAMES.get(role, role)} {number}" for role, number in (counts["modelCalls"] or {}).items()) or "—"
    tokens = (report["usage"] or {}).get("total_tokens")
    observed = report["testObservedBefore"]
    presentation = report.get("presentation") or {}  # reviews of jobs prepared without a template have none
    label, labels = presentation.get("metricLabel"), presentation.get("submetrics", {})
    lines = [
        f"Revisión del trabajo {report['jobId']} «{report['name']}»: {report['status']} · evaluación {'completa' if completeness['status'] == 'complete' else 'incompleta'}",
        f"Target: {report['target']['scope']}",
        f"Métrica principal: {metric['name']}{f' «{label}»' if label else ''} ({'más alto es mejor' if metric['higherIsBetter'] else 'más bajo es mejor'})"
        f" — {metric['definition']}",
        f"Submétricas: {', '.join(name + (f' «{labels[name]}»' if name in labels else '') for name in report['submetrics']) or '(ninguna)'}",
        f"Casos únicos (denominadores): train {cases['train']} · val {cases['val']} · test {cases['test']}",
        f"Evaluaciones: búsqueda {evaluations['search']} · prueba reservada {evaluations['final']} · llamadas: {calls} · iteraciones {counts['iterations']}"
        f" · comprobaciones internas {'no registradas' if internal['total'] is None else internal['total']} ({', '.join(internal['names']) or '—'}; no son casos)",
        f"Uso: tokens {tokens if tokens is not None else 'desconocidos'} · {_cost_text(report['costUsd'])}",
        (f"Prueba reservada ya observada: {observed['cases']} de {observed['total']} casos en {', '.join(item['jobId'] for item in observed['jobs'])}"
         if observed["observed"] else "Prueba reservada no observada en trabajos anteriores de esta carpeta de datos."),
    ]
    if presentation.get("notes"):
        lines.append(f"Notas de la plantilla {template_label(presentation['template'])}:")
        lines.extend(f"  · {note}" for note in presentation["notes"])
    if completeness["status"] != "complete":
        lines.append(completeness["message"])
    lines.append("Candidatos (validación · prueba reservada · frente al original en la prueba):")
    roles = {"original": "original", "finalist": "finalista", "candidate": "candidato"}
    for candidate in report["candidates"]:
        flags = roles[candidate["role"]] + (", seleccionado" if candidate["selected"] else "")
        line = (f"  - {candidate['label']} {candidate['id']} [{flags}]: validación {_review_measure(candidate['validation'])}"
                f" · prueba reservada {_review_measure(candidate['test'])}")
        test = candidate["test"]
        if test["submetrics"]:
            line += " · submétricas en la prueba: " + ", ".join(
                f"{name} {value['passed']}/{value['evaluated']}" if "passed" in value else f"{name} media {_mean_text(value['mean'])}"
                for name, value in test["submetrics"].items())
        for label, key in (("requisitos obligatorios cumplidos", "requirementsMet"), ("exactitud de tarea", "taskAccuracy")):
            if test.get(key):
                line += f" · {label} {test[key]['passed']}/{test[key]['evaluated']}"
        versus = (candidate["vsOriginal"] or {}).get("test")
        if versus:
            for word, key in (("mejora", "better"), ("empeora", "worse")):
                line += f" · {word} en {len(versus[key])}" + (f" ({_id_list(versus[key])})" if versus[key] else "")
        lines.append(line)
    lines.append(f"Recomendación: {report['recommendation']['message']}")
    lines.append("Límites de la evidencia:")
    lines.extend(f"  · {limit}" for limit in report["limits"])
    exports = [candidate["export"]["cli"] for candidate in report["candidates"] if candidate["export"]]
    if exports:
        lines.append("Exportar es una decisión explícita y nunca modifica el original:")
        lines.extend(f"  {command}" for command in exports)
    lines.append(f"Un caso: gepa job review {report['jobId']} --case <caseId> · todos: --cases · otros candidatos: --candidate <id> (hasta 5)")
    if "cases" in report:
        labels = {candidate["id"]: candidate["label"] for candidate in report["candidates"]}
        lines.append("Casos de validación y prueba reservada (score por candidato):")
        for row in report["cases"]:
            results = " · ".join(f"{labels[item['candidateId']]} {item['score']:g} ({_row_outcome(item)})" for item in row["results"])
            lines.append(f"  - {row['caseId']} [{row['split']}] esperado {_text(row['expected'], 120)} · {_text(row['input'], 120)} · {results or 'sin evaluar'}")
    if "case" in report:
        case = report["case"]
        lines.append(f"Caso {case['caseId']} [{case['split']}] · esperado {_text(case['expected'], 10_000)} · entrada {_text(case['input'], 10_000)}")
        lines.extend(_evaluation_text(item) for item in case["evaluations"])
        if not case["evaluations"]:
            lines.append("  (ningún candidato comparado evaluó este caso)")
    return "\n".join(lines)


def _problem_lines(problems: list[dict[str, Any]]) -> list[str]:
    return [f"  - [{item['code']}] {item['field']}: {item['message']}" for item in problems]


def render_template(view: dict[str, Any]) -> str:
    """Human summary of what ``gepa template check --json`` and ``gepa template save --json`` print."""
    if not view["valid"]:
        return "\n".join([f"Plantilla {view['path']}: no es válida", *_problem_lines(view["problems"]), f"Siguiente paso: {view['next']}"])
    adapter, evaluation, cases, presentation, origin = view["adapter"], view["evaluation"], view["cases"], view["presentation"], view["origin"]
    code = f" (sha256 {adapter['sha256'][:12]})" if "sha256" in adapter else ""  # an agent's adapter, in the template's adapter/ folder
    lines = [f"Plantilla «{view['name']}» v{view['version']} (sha256 {view['sha256'][:12]})", f"Carpeta: {view['path']}",
             *([f"Descripción: {view['description']}"] if view["description"] else []),
             *([f"Origen: dataset {origin.get('datasetId', '—')} (borrador {origin.get('draftId', '—')}) del proyecto {origin.get('project', '—')}"] if origin else []),
             f"Artefacto: {view['artifactType']} · adaptador {adapter['name']} v{adapter['version']}{code} · evaluador {evaluation['name']} v{evaluation['version']}",
             f"Criterio: {evaluation['description']}"]
    if "aggregation" in evaluation:
        lines.append(f"Regla de agregación por caso: {evaluation['aggregation']['rule']}")
    if view["executor"] is not None:
        lines.append("Ejecutor: " + ", ".join(f"{key} {value}" for key, value in view["executor"].items()))
    if view["evaluator"] is not None:
        lines.append(f"Ajustes del evaluador: {json.dumps(view['evaluator'], ensure_ascii=False)}")
    lines.append(f"Casos: campos {', '.join(cases['fields'])} · guía: {cases['guide'] or '(ninguna)'}")
    metric = f"«{presentation['metricLabel']}»" if presentation["metricLabel"] else "(como la nombra el evaluador)"
    labels = ", ".join(f"{name} «{label}»" for name, label in presentation["submetrics"].items()) or "(ninguna)"
    lines.append(f"Presentación: métrica {metric} · etiquetas: {labels}")
    lines.extend(f"  · {note}" for note in presentation["notes"])
    lines.append(f"Siguiente paso: {view['next']}")
    return "\n".join(lines)


def _template_line(item: dict[str, Any]) -> str:
    """One template of ``gepa template list``."""
    if not item["valid"]:
        return f"{item['path']} · no es válida: {len(item['problems'])} problemas (compruébala con gepa template check)"
    return f"{item['path']} · «{item['name']}» v{item['version']} · {item['artifactType']} · adaptador {item['adapter']['name']} · sha256 {item['sha256'][:12]}"


def main(argv: Sequence[str] | None = None, *, stdout: TextIO | None = None, stderr: TextIO | None = None, stdin: TextIO | None = None, probes: Probes | None = None,
         gateway: Gateway | None = None, launcher: Launcher | None = None) -> int:
    stdout = utf8(sys.stdout) if stdout is None else stdout
    stderr = utf8(sys.stderr) if stderr is None else stderr
    stdin = utf8(sys.stdin) if stdin is None else stdin
    parser = build_parser()
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):  # route argparse's help/version output to our streams
            args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    except _Usage as error:
        stderr.write(f"Uso incorrecto: {error}\n")
        parser.print_usage(stderr)
        return 2
    except SystemExit as exit_code:  # --help / --version
        return exit_code.code if isinstance(exit_code.code, int) else (0 if exit_code.code is None else 2)
    home = Path(args.home).expanduser().resolve() if args.home else default_home()
    try:
        return _dispatch(args, home, stdout, stderr, stdin, probes, gateway, launcher)
    except (ConfigError, JobError, ProviderError, ValueError) as error:
        stderr.write(f"Error: {error}\n")
        if getattr(args, "json", False):  # the same structured error an MCP tool returns
            _emit(stdout, error_payload(error), True)
        return 2


def _to_end(service: JobService, job_id: str, operation: Callable[[str], dict[str, Any]]) -> dict[str, Any]:
    """Run or retry a job here; on Ctrl+C report the job the engine marked ``interrupted`` (exit code 1) instead of a traceback."""
    try:
        return operation(job_id)
    except KeyboardInterrupt:
        return service.show(job_id)


def _engine_command(args: argparse.Namespace, home: Path, stdout: TextIO, stderr: TextIO, gateway: Gateway | None, launcher: Launcher | None) -> int:
    """``gepa adapter ...``, ``gepa dataset ...``, ``gepa template ...`` and ``gepa job ...``: thin translations to :class:`JobService`."""
    service = JobService.for_home(home, gateway=gateway, launcher=launcher)
    if args.command == "template":
        action = args.template_command
        if action == "save":
            request, path = load_json(args.request)
            view = service.template_save(request, base_dir=path.parent)
        elif action == "check":
            view = service.template_check(args.folder)
        elif action == "list":
            listing = service.template_list(artifact_type=args.artifact)
            _emit(stdout, listing, args.json, "\n".join([f"Carpeta de plantillas: {listing['folder']}", *map(_template_line, listing["templates"])])
                  if listing["templates"] else f"(sin plantillas en {listing['folder']})")
            return 0
        elif action == "init":
            view = service.template_init(args.example, path=args.folder)
            if not args.json:
                stdout.write(f"Plantilla de ejemplo {view['example']} copiada en {view['path']}.\n")
        elif action == "trust":
            trusted = service.template_trust(args.folder, sha256=args.sha256)
            _emit(stdout, trusted, args.json, f"Confías en el programa de {trusted['path']} (sha256 {trusted['sha256'][:12]}) desde {trusted['trustedAt']}.\n"
                                               f"Siguiente paso: {trusted['next']}")
            return 0
        else:
            build_parser().print_help(stdout)
            return 2
        _emit(stdout, view, args.json, render_template(view))
        return 0 if view["valid"] else 1
    if args.command == "adapter":
        if args.adapter_command == "init":
            result = service.adapter_init(args.folder, example=args.example)
            _emit(stdout, result, args.json, f"Adaptador de ejemplo {result['example']} copiado en {result['path']}: {', '.join(result['files'])}.\n"
                                              f"Siguiente paso: {result['next']}")
            return 0
        if args.adapter_command == "check":
            report = service.adapter_check(args.folder)
            _emit(stdout, report, args.json, render_adapter_check(report))
            return 0 if report["ready"] else 1
        build_parser().print_help(stdout)
        return 2
    if args.command == "dataset":
        action = args.dataset_command
        if action == "prepare":
            request, path = load_json(args.request)
            view = service.prepare(request, base_dir=path.parent)
        elif action == "preview":
            preview = service.preview(args.draft_id, sample=args.sample, decider=args.decider, judge=args.judge)
            _emit(stdout, preview, args.json, render_preview(preview))
            return 0 if preview["status"] == "complete" else 1
        elif action == "approve":
            view = service.approve(args.draft_id, reviewed_all_cases=args.reviewed_all)
        elif action == "show":
            view = service.dataset(args.dataset_id, cases=args.cases)
        else:
            build_parser().print_help(stdout)
            return 2
        _emit(stdout, view, args.json, render_dataset(view))
        return 0 if view["ready"] else 1
    action = args.job_command
    if action == "start":
        spec, path = load_json(args.spec)
        if args.detach:
            view = service.start(spec, base_dir=path.parent, detach=True)
        else:
            view = service.create(spec, base_dir=path.parent)
            stderr.write(f"Trabajo {view['jobId']} en curso; puedes consultarlo con: gepa job show {view['jobId']}\n")
            stderr.flush()
            view = _to_end(service, view["jobId"], service.run)
    elif action == "run":
        view = _to_end(service, args.job_id, service.run)
    elif action == "cancel":
        view = service.cancel(args.job_id)
        _emit(stdout, view, args.json, render_job(view))
        return 0
    elif action == "retry":
        retry = partial(service.retry, kind=args.kind)
        view = retry(args.job_id, detach=True) if args.detach else _to_end(service, args.job_id, retry)
    elif action == "show":
        view = service.show(args.job_id, cases=args.cases)
    elif action == "list":
        listing = service.list()
        _emit(stdout, listing, args.json, "\n".join(f"{job['jobId']} · {job['status']} · {job['name']} · {job['createdAt']}" for job in listing["jobs"]) or "(sin trabajos)")
        return 0
    elif action == "review":
        review = service.review(args.job_id, candidate_ids=args.candidates, case_id=args.case, cases=args.cases)
        _emit(stdout, review, args.json, render_review(review))
        return 0
    elif action == "export":
        report = service.export(args.job_id, args.out, candidate_id=args.candidate)
        _emit(stdout, report, args.json, f"Exportado {report['candidateId']} en {report['outDir']}: {', '.join(report['files'])}. El original no se modificó.")
        return 0
    else:
        build_parser().print_help(stdout)
        return 2
    _emit(stdout, view, args.json, render_job(view))
    return 0 if view["status"] in ("completed", "queued", "running") else 1


def _dispatch(args: argparse.Namespace, home: Path, stdout: TextIO, stderr: TextIO, stdin: TextIO, probes: Probes | None,
              gateway: Gateway | None = None, launcher: Launcher | None = None) -> int:
    if args.command == "mcp":
        from .mcp_server import serve

        serve(home=home, stdin=stdin, stdout=stdout, probes=probes, gateway=gateway, launcher=launcher)
        return 0
    if args.command in ("adapter", "dataset", "job", "template"):
        return _engine_command(args, home, stdout, stderr, gateway, launcher)
    if args.command != "setup" or not args.setup_command:
        build_parser().print_help(stdout)
        return 0 if args.command is None else 2
    action = args.setup_command
    settings = load_settings(home)

    if action == "init":
        settings = default_settings(home) if not settings.config_path.exists() else settings
        if args.data_dir:
            settings = with_data_dir(settings, args.data_dir)
        path = save_settings(settings)
        stdout.write(f"Configuración creada en {path}. Datos en {settings.data_dir}.\nSiguiente paso: gepa setup connection add ... y luego gepa setup check.\n")
        return 0

    if action == "show":
        view = public_view(settings, open_secret_store(settings))
        connections = [f"  - {c['id']} [{c['provider']}] {c['model']} @ {c['url']} · credencial: {'sí' if c['hasKey'] else 'no'}" + (f" (variable {c['apiKeyEnv']})" if c["apiKeyEnv"] else "") for c in view["connections"]] or ["  (ninguna)"]
        dependencies = [f"  - {d['kind']}:{d['name']}" + (f" — {d['purpose']}" if d["purpose"] else "") for d in view["dependencies"]] or ["  (ninguna)"]
        folders = view["folders"]
        pending = "" if folders["designated"] else " (propuestas, sin designar: gepa setup folders)"
        human = "\n".join([
            f"Configuración: {view['configPath']}", f"Datos: {view['dataDir']}", f"Motor: {view['engine']['package']} {view['engine']['version']}",
            f"Proyecto: {folders['project']} · plantillas: {folders['templates']} · adaptadores propios: {folders['adapters']}{pending}",
            "Conexiones:", *connections,
            "Roles:", *(f"  - {role}: {target or '(sin asignar)'}" for role, target in view["roles"].items()),
            "Dependencias declaradas:", *dependencies,
        ])
        _emit(stdout, view, args.json, human)
        return 0

    if action == "check":
        report = run_doctor(settings, probes or real_probes(settings))
        _emit(stdout, report, args.json, render_report(report))
        return 0 if report["ready"] else 1

    if action == "data-dir":
        settings = with_data_dir(settings, args.path)
        save_settings(settings)
        stdout.write(f"Carpeta de datos: {settings.data_dir}\n")
        return 0

    if action == "folders":
        settings, report = designate(settings, templates=args.templates, adapters=args.adapters)
        save_settings(settings)
        ignored = report["gitignore"]
        rules = ", ".join(line for line in ignored["added"] if not line.startswith("#")) or "ya estaban todas"
        _emit(stdout, report, args.json, f"Plantillas: {report['folders']['templates']}\nAdaptadores propios: {report['folders']['adapters']}\n"
                                         f"{ignored['path']} ({'creado' if ignored['created'] else 'conservado'}): reglas añadidas: {rules}.")
        return 0

    if action == "role":
        settings = with_role(settings, args.role, args.connection_id)
        save_settings(settings)
        stdout.write(f"Rol {args.role} -> conexión {args.connection_id}\n")
        return 0

    if action == "connection":
        sub = args.connection_command
        if sub == "add":
            connection = make_connection(identifier=args.id, provider=args.provider, model=args.model, url=args.url, name=args.name, api_key_env=args.api_key_env)
            settings = with_connection(settings, connection)
            save_settings(settings)
            stdout.write(f"Conexión {connection.id} guardada: {connection.provider} {connection.model} @ {connection.url}\n")
            return 0
        if sub == "remove":
            settings = without_connection(settings, args.connection_id)
            save_settings(settings)
            open_secret_store(settings).delete(args.connection_id)
            stdout.write(f"Conexión {args.connection_id} eliminada.\n")
            return 0
        if sub == "set-key":
            if settings.connection(args.connection_id) is None:
                raise ConfigError(f"No existe la conexión '{args.connection_id}'.")
            key = (stdin.readline() if not stdin.isatty() else getpass.getpass("API key (no se mostrará): ", stream=stderr)).strip()
            if not key:
                raise ConfigError("No se recibió ninguna API key.")
            store = open_secret_store(settings)
            store.set(args.connection_id, key)
            if not store.persistent:
                stderr.write("Aviso: en este sistema el almacén cifrado no persiste; usa --api-key-env con una variable de entorno.\n")
                return 2
            stdout.write(f"Credencial de {args.connection_id} guardada cifrada para el usuario actual.\n")
            return 0
        view = public_view(settings, open_secret_store(settings))
        _emit(stdout, {"connections": view["connections"]}, True)
        return 0

    if action == "dependency":
        settings = with_dependency(settings, make_dependency(kind=args.kind, name=args.name, purpose=args.purpose))
        save_settings(settings)
        stdout.write(f"Dependencia declarada: {args.kind}:{args.name}\n")
        return 0

    if action == "install":
        report = install(host=args.host, scope=args.scope, target=args.target, mcp=not args.no_mcp, home=args.home or os.environ.get("GEPA_HOME") or None)
        _emit(stdout, report, args.json, "\n".join(report["instructions"]))
        return 0

    build_parser().print_help(stdout)
    return 2
