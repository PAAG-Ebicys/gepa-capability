"""Minimal MCP server over stdio (JSON-RPC 2.0, newline-delimited) with no third-party dependency.

Each tool is a thin translation to the same operations the CLI runs, so a host with native
tools and a host that only has a terminal see identical reports.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, Mapping, TextIO

from . import __version__
from .artifacts import load_json
from .config import ConfigError, load_settings, open_secret_store, public_view
from .declaration import DEFAULT_EXAMPLE
from .doctor import Probes, real_probes, run_doctor
from .encoding import utf8
from .jobs import PREVIEW_SAMPLE, Gateway, JobError, JobService, Launcher, error_payload
from .lifecycle import RETRY_KINDS
from .providers import ProviderError
from .templates import examples as template_examples

PROTOCOL_VERSION = "2025-06-18"
EMPTY_OBJECT_SCHEMA = {"type": "object", "properties": {}, "additionalProperties": False}
JSON_TYPES: dict[str, type] = {"string": str, "boolean": bool, "integer": int, "object": dict, "array": list}


ToolHandler = Callable[[dict[str, Any]], dict[str, Any]]


def _schema(properties: dict[str, Any], required: tuple[str, ...] = ()) -> dict[str, Any]:
    return {"type": "object", "properties": properties, "required": list(required), "additionalProperties": False}


def _arguments(arguments: dict[str, Any], schema: Mapping[str, Any]) -> dict[str, Any]:
    """Reject unknown, missing or mistyped arguments with a readable tool error."""
    properties = schema["properties"]
    unknown = set(arguments) - set(properties)
    missing = [key for key in schema.get("required", ()) if key not in arguments]
    if unknown or missing:
        raise JobError(f"Argumentos inválidos: faltan {missing or 'ninguno'}, sobran {sorted(unknown) or 'ninguno'}.", "invalid-arguments")
    for key, value in arguments.items():
        expected = properties[key]["type"]
        # ``True`` is an ``int`` in Python, not an integer in JSON.
        if not isinstance(value, JSON_TYPES[expected]) or (expected == "integer" and isinstance(value, bool)):
            raise JobError(f"El argumento '{key}' debe ser de tipo {expected}.", "invalid-arguments")
        if "enum" in properties[key] and value not in properties[key]["enum"]:
            raise JobError(f"El argumento '{key}' debe ser uno de: {', '.join(properties[key]['enum'])}.", "invalid-arguments")
        items = properties[key].get("items", {}).get("type")
        if isinstance(value, list) and items and not all(isinstance(item, JSON_TYPES[items]) for item in value):
            raise JobError(f"Cada elemento de '{key}' debe ser de tipo {items}.", "invalid-arguments")
    return arguments


def _tools(home: Path | None, probes: Probes | None, gateway: Gateway | None = None, launcher: Launcher | None = None) -> dict[str, tuple[dict[str, Any], ToolHandler]]:
    def check(_: dict[str, Any]) -> dict[str, Any]:
        settings = load_settings(home)
        return run_doctor(settings, probes or real_probes(settings))

    def show(_: dict[str, Any]) -> dict[str, Any]:
        settings = load_settings(home)
        return public_view(settings, open_secret_store(settings))

    def service() -> JobService:
        # Jobs started from a tool run in their own process, so a host's tool timeout never cuts the search.
        return JobService.for_home(home, gateway=gateway, launcher=launcher)

    adapter_init_schema = _schema({"path": {"type": "string", "description": "Carpeta nueva o vacía (ruta absoluta) donde copiar el ejemplo."},
                                   "example": {"type": "string", "description": f"Ejemplo a copiar; por defecto {DEFAULT_EXAMPLE}."}}, ("path",))
    adapter_check_schema = _schema({"path": {"type": "string", "description": "Carpeta del adaptador (ruta absoluta), con adapter.json y adapter.py."}}, ("path",))
    prepare_schema = _schema({"requestPath": {"type": "string", "description": "Solicitud JSON de preparación; sus rutas relativas se resuelven desde su carpeta."},
                              "request": {"type": "object", "description": "Solicitud en línea, si no hay archivo (usa rutas absolutas)."}})
    preview_schema = _schema({"draftId": {"type": "string"},
                              "sample": {"type": "integer", "description": f"Casos de train y val que ejecuta el original (por defecto {PREVIEW_SAMPLE}; al menos uno por opción)."},
                              "decider": {"type": "string", "description": "Conexión del modelo que ejecuta el original (decisor JEV o ejecutor de la Skill); por defecto, el rol executor."},
                              "judge": {"type": "string", "description": "Conexión del juez con el evaluador rubric-judge; por defecto, el rol judge."}}, ("draftId",))
    approve_schema = _schema({"draftId": {"type": "string"},
                              "reviewedAllCases": {"type": "boolean", "description": "true solo si la persona revisó el dataset completo; queda registrado."}}, ("draftId",))
    dataset_schema = _schema({"datasetId": {"type": "string", "description": "Un borrador (draft-…) o una versión aprobada (ds-…)."},
                              "cases": {"type": "boolean", "description": "Incluir todos los casos, para revisarlos completos."}}, ("datasetId",))
    start_schema = _schema({"specPath": {"type": "string", "description": "Especificación JSON del trabajo; las rutas relativas se resuelven desde su carpeta."},
                            "spec": {"type": "object", "description": "Especificación en línea, si no hay archivo (usa rutas absolutas)."}})
    job_schema = _schema({"jobId": {"type": "string"}, "cases": {"type": "boolean", "description": "Incluir la evidencia por caso."}}, ("jobId",))
    job_id_schema = _schema({"jobId": {"type": "string"}}, ("jobId",))
    retry_schema = _schema({"jobId": {"type": "string"},
                            "kind": {"type": "string", "enum": list(RETRY_KINDS), "description": "La recuperación que eligió la persona, una de recovery.options: "
                                                                                                  "obligatoria cuando el trabajo ofrece dos (continue-search o final-retry)."}},
                           ("jobId",))
    review_schema = _schema({"jobId": {"type": "string"},
                             "candidateIds": {"type": "array", "items": {"type": "string"}, "description": "Candidatos a comparar con el original (hasta 5); por defecto, los finalistas."},
                             "caseId": {"type": "string", "description": "Un caso: toda su evidencia guardada (entrada, salida, score, feedback, errores y traza)."},
                             "cases": {"type": "boolean", "description": "Incluir una fila por caso de validación y de prueba reservada."}}, ("jobId",))
    export_schema = _schema({"jobId": {"type": "string"}, "outDir": {"type": "string", "description": "Carpeta nueva o vacía."},
                             "candidateId": {"type": "string", "description": "Finalista a exportar; por defecto, el seleccionado por validación."}}, ("jobId", "outDir"))
    template_save_schema = _schema({"requestPath": {"type": "string", "description": "Solicitud JSON: from (el ds-… aprobado), name y, opcionales, description, "
                                                                                  "cases.guide, presentation y path (la carpeta nueva; una ruta relativa se "
                                                                                  "resuelve desde la carpeta de la solicitud)."},
                                    "request": {"type": "object", "description": "Solicitud en línea, si no hay archivo (path, absoluta)."}})
    template_check_schema = _schema({"path": {"type": "string", "description": "Carpeta de la plantilla (ruta absoluta), con template.json."}}, ("path",))
    template_list_schema = _schema({"artifact": {"type": "string", "description": "Solo las plantillas de este tipo de artefacto (jev-policy o skill)."}})
    template_init_schema = _schema({"example": {"type": "string", "enum": template_examples(), "description": "Plantilla de ejemplo a copiar."},
                                    "path": {"type": "string", "description": "Carpeta nueva (ruta absoluta); por defecto, una con el nombre del ejemplo "
                                                                              "en la carpeta de plantillas del proyecto."}}, ("example",))

    def document(arguments: dict[str, Any], path_key: str, inline_key: str) -> tuple[Any, Path | None]:
        """A JSON document given by file (relative paths inside it resolve from its folder) or inline."""
        if (path_key in arguments) == (inline_key in arguments):
            raise JobError(f"Indica '{path_key}' o '{inline_key}', no ambos.", "invalid-arguments")
        if path_key in arguments:
            loaded, path = load_json(arguments[path_key])
            return loaded, path.parent
        return arguments[inline_key], None

    def init_adapter(arguments: dict[str, Any]) -> dict[str, Any]:
        _arguments(arguments, adapter_init_schema)
        return service().adapter_init(arguments["path"], example=arguments.get("example", DEFAULT_EXAMPLE))

    def check_adapter(arguments: dict[str, Any]) -> dict[str, Any]:
        _arguments(arguments, adapter_check_schema)
        return service().adapter_check(arguments["path"])

    def prepare_dataset(arguments: dict[str, Any]) -> dict[str, Any]:
        _arguments(arguments, prepare_schema)
        request, base_dir = document(arguments, "requestPath", "request")
        return service().prepare(request, base_dir=base_dir)

    def preview_dataset(arguments: dict[str, Any]) -> dict[str, Any]:
        _arguments(arguments, preview_schema)
        return service().preview(arguments["draftId"], sample=arguments.get("sample"), decider=arguments.get("decider"), judge=arguments.get("judge"))

    def approve_dataset(arguments: dict[str, Any]) -> dict[str, Any]:
        _arguments(arguments, approve_schema)
        return service().approve(arguments["draftId"], reviewed_all_cases=bool(arguments.get("reviewedAllCases")))

    def show_dataset(arguments: dict[str, Any]) -> dict[str, Any]:
        _arguments(arguments, dataset_schema)
        return service().dataset(arguments["datasetId"], cases=bool(arguments.get("cases")))

    def start_job(arguments: dict[str, Any]) -> dict[str, Any]:
        _arguments(arguments, start_schema)
        spec, base_dir = document(arguments, "specPath", "spec")
        return service().start(spec, base_dir=base_dir, detach=True)

    def show_job(arguments: dict[str, Any]) -> dict[str, Any]:
        _arguments(arguments, job_schema)
        return service().show(arguments["jobId"], cases=bool(arguments.get("cases")))

    def list_jobs(arguments: dict[str, Any]) -> dict[str, Any]:
        _arguments(arguments, EMPTY_OBJECT_SCHEMA)
        return service().list()

    def review_job(arguments: dict[str, Any]) -> dict[str, Any]:
        _arguments(arguments, review_schema)
        return service().review(arguments["jobId"], candidate_ids=arguments.get("candidateIds"), case_id=arguments.get("caseId"), cases=bool(arguments.get("cases")))

    def export_job(arguments: dict[str, Any]) -> dict[str, Any]:
        _arguments(arguments, export_schema)
        return service().export(arguments["jobId"], arguments["outDir"], candidate_id=arguments.get("candidateId"))

    def cancel_job(arguments: dict[str, Any]) -> dict[str, Any]:
        _arguments(arguments, job_id_schema)
        return service().cancel(arguments["jobId"])

    def retry_job(arguments: dict[str, Any]) -> dict[str, Any]:
        _arguments(arguments, retry_schema)
        return service().retry(arguments["jobId"], kind=arguments.get("kind"), detach=True)

    def save_template(arguments: dict[str, Any]) -> dict[str, Any]:
        _arguments(arguments, template_save_schema)
        request, base_dir = document(arguments, "requestPath", "request")
        return service().template_save(request, base_dir=base_dir)

    def check_template(arguments: dict[str, Any]) -> dict[str, Any]:
        _arguments(arguments, template_check_schema)
        return service().template_check(arguments["path"])

    def list_templates(arguments: dict[str, Any]) -> dict[str, Any]:
        _arguments(arguments, template_list_schema)
        return service().template_list(artifact_type=arguments.get("artifact"))

    def init_template(arguments: dict[str, Any]) -> dict[str, Any]:
        _arguments(arguments, template_init_schema)
        return service().template_init(arguments["example"], path=arguments.get("path"))

    definitions: list[tuple[str, str, str, dict[str, Any], ToolHandler]] = [
        ("gepa_setup_check", "Comprobar la instalación de GEPA", "Diagnostica motor, dependencias, carpeta de datos, conexiones a modelos, credenciales y roles. Devuelve hallazgos con código y siguiente paso; nunca incluye credenciales.", EMPTY_OBJECT_SCHEMA, check),
        ("gepa_setup_show", "Mostrar la configuración de GEPA", "Muestra la configuración actual (ubicación de datos, conexiones, modelos y roles) sin credenciales.", EMPTY_OBJECT_SCHEMA, show),
        ("gepa_adapter_init", "Copiar un adaptador de ejemplo", "Copia un adaptador de experimento de ejemplo (adapter.json y adapter.py) en una carpeta nueva, para adaptarlo a una tarea que los adaptadores incluidos no cubren: otra forma de ejecutar o de evaluar, con código, navegador, API o sandbox.", adapter_init_schema, init_adapter),
        ("gepa_adapter_check", "Comprobar un adaptador", "Valida adapter.json y adapter.py y diagnostica, sin llamar a modelos, cada requisito declarado (módulo Python, comando, credencial) y la comprobación propia del adaptador (navegador, API, sandbox). Cada hallazgo trae el siguiente paso y quién puede darlo: el agente o la persona; nunca muestra credenciales.", adapter_check_schema, check_adapter),
        ("gepa_dataset_prepare", "Preparar casos para aprobar", "Para una política JEV o una Skill (carpeta con SKILL.md), importa casos aportados, celdas pegadas de Excel o archivos Excel, CSV o JSON; registra su procedencia, valida id, contenido que exige el evaluador, particiones y casos duplicados o parecidos entre particiones. Una Skill puede elegir evaluador: comprobaciones (file-checks) o rúbrica con juez (rubric-judge), con comprobaciones obligatorias. Con 'adapter' usa un adaptador propio y congela una copia suya identificada por su hash. Con 'template' ({\"path\": \"<carpeta>\"}) comprueba esa carpeta de plantilla sobre el original, toma de ella el adaptador, el ejecutor y el evaluador, congela una copia identificada por su SHA-256 y la registra en el borrador, que siempre es nuevo y necesita su previsualización y su aprobación. Rechaza una copia para compartir (template-is-share-copy) y, antes de cargarlo, el programa propio de una plantilla que este proyecto nunca ejecutó ni la persona confió (template-untrusted): el error trae en trust.command el comando que la persona ejecuta en su terminal para confiar; el agente nunca lo ejecuta. Devuelve el borrador con objetivo, cobertura, criterios o ejecutor, evaluador con su regla de agregación y todos los problemas; no llama a modelos.", prepare_schema, prepare_dataset),
        ("gepa_dataset_preview", "Previsualizar el original", "Ejecuta el original (política o Skill) sobre algunos casos de train y val del borrador (nunca de test) y devuelve salida, score, submétricas y feedback por caso; en una Skill, también los archivos producidos, la sesión y, con rubric-judge, el veredicto del juez por criterio. Aprobar exige una previsualización completa.", preview_schema, preview_dataset),
        ("gepa_dataset_approve", "Aprobar una versión del dataset", "Sella un borrador previsualizado: casos, artefacto (política o Skill con sus recursos), adaptador (el propio, por el hash de su carpeta), evaluador, ejecutor y su código quedan en una versión inmutable (ds-…). Un cambio posterior produce otro borrador que necesita su propia aprobación.", approve_schema, approve_dataset),
        ("gepa_dataset_show", "Mostrar un borrador o un dataset aprobado", "Muestra resumen, cobertura, problemas y previsualización; con cases=true incluye todos los casos para revisarlos completos.", dataset_schema, show_dataset),
        ("gepa_template_save", "Guardar una plantilla de experimento", "Guarda la clase de tarea de un dataset aprobado (ds-…) como carpeta de plantilla nueva, versión 1, en la carpeta de plantillas del proyecto o en 'path': template.json con tipo de artefacto, adaptador, ajustes del ejecutor y del evaluador, guía de casos y reglas de presentación, y, con un adaptador propio, sus archivos en adapter/. Nunca sobrescribe una carpeta (template-exists) y nunca guarda casos (ni respuestas de la prueba reservada), previsualizaciones, resultados, modelos ni credenciales (secret-in-template).", template_save_schema, save_template),
        ("gepa_template_check", "Comprobar una plantilla", "Lee una carpeta de plantilla sin el original y sin cargar ni ejecutar el programa de su adaptador. Devuelve lo que el motor deduce (el evaluador con su regla de agregación y los campos de los casos), su SHA-256 y la solicitud de preparación que la aplica; o, si hay problemas, cada uno con su campo y su código. Úsala tras editar los archivos de una plantilla.", template_check_schema, check_template),
        ("gepa_template_list", "Listar plantillas de experimento", "Lista las carpetas de la carpeta de plantillas del proyecto con su nombre, descripción, versión, tipo de artefacto, adaptador y SHA-256, para encontrar una que sirva a una tarea nueva. Omite las copias para compartir (<nombre>.compartir/), que nunca se aplican.", template_list_schema, list_templates),
        ("gepa_template_init", "Copiar una plantilla de ejemplo", "Copia una plantilla de ejemplo de GEPA (skill-file-checks: una Skill con comprobaciones de archivos; jev-policy: una política JEV) en una carpeta nueva, por defecto dentro de la carpeta de plantillas del proyecto, para adaptarla. Usa un adaptador incluido y ningún programa propio. Nunca sobrescribe una carpeta (template-exists). Devuelve la vista de gepa_template_check.", template_init_schema, init_template),
        ("gepa_job_start", "Iniciar un trabajo de optimización", "Congela el manifiesto (política o Skill, dataset, modelos, evaluador, superficie mutable y límites) y ejecuta la búsqueda GEPA en un proceso aparte. Devuelve el jobId en cola; consulta con gepa_job_show.", start_schema, start_job),
        ("gepa_job_show", "Consultar un trabajo", "Estado, fase, intentos, eventos, candidatos con puntuación de validación y de prueba reservada, selección, consumo por fase (búsqueda y prueba reservada, con sus límites) y qué se puede reintentar. Un trabajo cuyo proceso terminó sin cerrarlo aparece como interrupted. Con cases=true incluye la evidencia por caso.", job_schema, show_job),
        ("gepa_job_cancel", "Cancelar un trabajo", "Cancela un trabajo desde cualquier sesión: en cola, al momento; en curso, antes de su siguiente llamada a un modelo (la llamada en curso termina). Conserva candidatos y evidencia, y no marca completa una fase con casos pendientes.", job_id_schema, cancel_job),
        ("gepa_job_retry", "Reintentar un trabajo detenido", "Reintenta en un proceso aparte un trabajo failed, stopped, cancelled o interrupted, con una de las recuperaciones de recovery.options. Con la selección congelada, final-retry evalúa solo los casos pendientes de la prueba reservada, sin repetir GEPA. Tras una búsqueda cortada con la validación del original completa hay dos, y kind es obligatorio (recovery-choice-required): pregunta antes a la persona. continue-search continúa GEPA desde su último estado guardado, sin repetir las iteraciones completas, y después evalúa la prueba reservada; final-retry elige entre los candidatos ya validados y evalúa la prueba reservada. Si se detuvo antes de la búsqueda, run lo ejecuta completo. Lo que gastaron los intentos anteriores sigue contabilizado. Un estado de GEPA que falta, está dañado o no es del trabajo se rechaza (search-state-missing, search-state-damaged, search-state-mismatch). Sin la validación del original (search-not-recoverable) se crea un trabajo nuevo.", retry_schema, retry_job),
        ("gepa_job_list", "Listar trabajos", "Lista los trabajos con su estado y candidato seleccionado.", EMPTY_OBJECT_SCHEMA, list_jobs),
        ("gepa_job_review", "Revisar la evidencia de un trabajo", "Compara el original con hasta cinco candidatos sobre los mismos casos a partir de la evidencia guardada, sin llamar a modelos: métrica principal, submétricas, cobertura, completitud, consumo, target, test ya observado y recomendación (conservar el original salvo mejora demostrada). caseId abre un caso; cases=true da una fila por caso.", review_schema, review_job),
        ("gepa_job_export", "Exportar un candidato", "Escribe el finalista elegido (policy.json, o la carpeta skill/ con su SKILL.md y los recursos originales), manifest.json y evidence.json en una carpeta nueva; nunca modifica el original.", export_schema, export_job),
    ]
    return {name: ({"name": name, "title": title, "description": description, "inputSchema": schema}, handler) for name, title, description, schema, handler in definitions}


def _result(identifier: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": identifier, "result": result}


def _error(identifier: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": identifier, "error": {"code": code, "message": message}}


def _tool_result(payload: dict[str, Any], *, is_error: bool = False) -> dict[str, Any]:
    return {"content": [{"type": "text", "text": json.dumps(payload, ensure_ascii=False, indent=2)}], "structuredContent": payload, "isError": is_error}


def handle(message: dict[str, Any], tools: Mapping[str, tuple[dict[str, Any], ToolHandler]]) -> dict[str, Any] | None:
    identifier = message.get("id")
    method = message.get("method")
    if not isinstance(method, str):
        return _error(identifier, -32600, "Solicitud inválida: falta 'method'.")
    if method.startswith("notifications/"):
        return None
    params = message.get("params") or {}
    if method == "initialize":
        return _result(identifier, {"protocolVersion": PROTOCOL_VERSION, "capabilities": {"tools": {"listChanged": False}}, "serverInfo": {"name": "gepa", "version": __version__},
                                    "instructions": "Herramientas GEPA. gepa_setup_check diagnostica el entorno; gepa_dataset_prepare, gepa_dataset_preview y gepa_dataset_approve preparan, prueban con el original y sellan los casos; gepa_template_save, gepa_template_check, gepa_template_list y gepa_template_init guardan, comprueban, encuentran y copian de un ejemplo plantillas de experimento, que son carpetas del proyecto y se aplican con 'template' (la ruta de su carpeta) en gepa_dataset_prepare; gepa_job_start inicia una optimización; gepa_job_show la consulta; gepa_job_cancel la cancela; gepa_job_retry reintenta un trabajo detenido: la prueba reservada pendiente, o una búsqueda cortada, que continúa desde el último estado guardado de GEPA o cierra con los candidatos validados, según elija la persona; gepa_job_review explica la evidencia por caso y recomienda, sin llamar a modelos; gepa_job_export exporta un finalista sin tocar el original."})
    if method == "ping":
        return _result(identifier, {})
    if method == "tools/list":
        return _result(identifier, {"tools": [definition for definition, _ in tools.values()]})
    if method == "tools/call":
        name = params.get("name")
        if not isinstance(name, str) or name not in tools:
            return _result(identifier, _tool_result({"error": f"Herramienta desconocida: {name}"}, is_error=True))
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            return _result(identifier, _tool_result({"error": "Los argumentos deben ser un objeto.", "code": "invalid-arguments"}, is_error=True))
        try:
            return _result(identifier, _tool_result(tools[name][1](arguments)))
        except (ConfigError, JobError, ProviderError) as error:
            return _result(identifier, _tool_result(error_payload(error), is_error=True))
        except Exception as error:  # a failing tool must never take the server (and every later request) down
            return _result(identifier, _tool_result({"error": f"Error interno de la herramienta ({type(error).__name__}).", "code": "internal-error"}, is_error=True))
    return _error(identifier, -32601, f"Método no soportado: {method}")


def serve(*, home: Path | None = None, stdin: TextIO | None = None, stdout: TextIO | None = None, probes: Probes | None = None,
          gateway: Gateway | None = None, launcher: Launcher | None = None) -> None:
    stdin = utf8(sys.stdin) if stdin is None else stdin
    stdout = utf8(sys.stdout) if stdout is None else stdout
    tools = _tools(home, probes, gateway, launcher)
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        reply: dict[str, Any] | None
        try:
            message = json.loads(line)
            if not isinstance(message, dict):
                raise ValueError()
        except ValueError:
            reply = _error(None, -32700, "No se pudo interpretar el mensaje como JSON.")
        else:
            reply = handle(message, tools)
        if reply is not None:
            stdout.write(json.dumps(reply, ensure_ascii=False) + "\n")
            stdout.flush()
