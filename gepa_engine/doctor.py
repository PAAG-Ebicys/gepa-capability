"""``gepa setup check``: classify what is missing and say exactly what to do next.

Every probe that touches the machine or the network is injectable, so the whole
diagnosis is testable without a model server, credentials or the real ``gepa`` package.

Report semantics: ``ok`` means no error was found; ``ready`` additionally requires the
three roles to point at connections whose small inference run succeeded. Only ``ready``
means GEPA can actually optimize something.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import platform
import shutil
import sys
from dataclasses import asdict, dataclass
from typing import Any, Callable, Mapping

from .config import ENGINE_PACKAGE, ENGINE_VERSION, ROLES, Connection, Settings, credential_lookup, has_credential, open_secret_store, resolved_folders
from .files import open_new
from .providers import ModelGateway, ProviderError

BASE_PYTHON_DEPENDENCIES = ({"name": "httpx", "purpose": "hablar con los proveedores de modelos"},)
SMOKE_MESSAGES = [{"role": "user", "content": "Responde únicamente con la palabra: listo"}]
SMOKE_MAX_TOKENS = 16
SMOKE_TIMEOUT_SECONDS = 60


@dataclass(frozen=True)
class Finding:
    component: str
    status: str  # ok | warning | error
    code: str
    message: str
    nextStep: str = ""


@dataclass(frozen=True)
class Probes:
    """Side-effecting lookups the diagnosis depends on; tests substitute each one."""

    engine_version: Callable[[], str | None]
    python_module: Callable[[str], bool]
    command_path: Callable[[str], str | None]
    catalog: Callable[[Connection], list[dict[str, Any]]]
    complete: Callable[[Connection], dict[str, Any]]
    has_credential: Callable[[Connection], bool]
    encrypted_store: Callable[[], bool]  # whether set-key keeps a credential across sessions (Windows DPAPI); elsewhere only variables do


def _installed_version(package: str) -> str | None:
    try:
        return importlib.metadata.version(package)
    except importlib.metadata.PackageNotFoundError:
        return None


def real_probes(settings: Settings, environ: Mapping[str, str] | None = None) -> Probes:
    environ = os.environ if environ is None else environ
    secrets = open_secret_store(settings)
    gateway = ModelGateway(credential_lookup(settings, secrets, environ))
    return Probes(
        engine_version=lambda: _installed_version(ENGINE_PACKAGE),
        python_module=lambda name: importlib.util.find_spec(name) is not None,
        command_path=shutil.which,
        catalog=lambda connection: gateway.models(connection.as_provider_dict()),
        complete=lambda connection: gateway.complete(connection.as_provider_dict(), SMOKE_MESSAGES, max_tokens=SMOKE_MAX_TOKENS, timeout=SMOKE_TIMEOUT_SECONDS),
        has_credential=lambda connection: has_credential(connection, secrets, environ),
        encrypted_store=lambda: secrets.persistent,
    )


def _pip(package: str) -> str:
    return f"{sys.executable} -m pip install {package}"


def _check_engine(probes: Probes) -> list[Finding]:
    version = probes.engine_version()
    if version is None:
        return [Finding("engine", "error", "engine-missing", f"El motor GEPA (paquete Python '{ENGINE_PACKAGE}') no está instalado en este intérprete.", f"Instálalo con: {_pip(f'{ENGINE_PACKAGE}=={ENGINE_VERSION}')} y vuelve a ejecutar 'gepa setup check'.")]
    if version != ENGINE_VERSION:
        return [Finding("engine", "warning", "engine-version", f"El motor GEPA instalado es {version}; esta capacidad se comprobó con {ENGINE_VERSION}.", f"Si algo falla, fija la versión con: {_pip(f'{ENGINE_PACKAGE}=={ENGINE_VERSION}')}.")]
    return [Finding("engine", "ok", "engine-ok", f"Motor GEPA {version} disponible.")]


def _check_dependencies(settings: Settings, probes: Probes) -> list[Finding]:
    findings = []
    declared = [("python", d["name"], d["purpose"]) for d in BASE_PYTHON_DEPENDENCIES] + [(d.kind, d.name, d.purpose) for d in settings.dependencies]
    for kind, name, purpose in declared:
        component = f"{kind}:{name}"
        why = f" (necesaria para {purpose})" if purpose else ""
        if kind == "python":
            if probes.python_module(name):
                findings.append(Finding(component, "ok", "dependency-ok", f"Módulo Python '{name}' disponible."))
            else:
                findings.append(Finding(component, "error", "dependency-missing", f"Falta el módulo Python '{name}'{why}.", f"Instálalo con: {_pip(name)}."))
        else:
            location = probes.command_path(name)
            if location:
                findings.append(Finding(component, "ok", "dependency-ok", f"Comando '{name}' disponible en {location}."))
            else:
                findings.append(Finding(component, "error", "dependency-missing", f"Falta el comando '{name}'{why}.", f"Instala '{name}' y asegúrate de que esté en el PATH; luego repite 'gepa setup check'."))
    return findings


def _check_data_dir(settings: Settings) -> list[Finding]:
    path = settings.data_dir
    try:
        path.mkdir(parents=True, exist_ok=True)
        handle, probe = open_new(path, ".write-check-", text=False)
        handle.close()
        probe.unlink()
    except OSError:
        return [Finding("data-dir", "error", "data-dir-unwritable", f"No se puede escribir en la carpeta de datos {path}.", "Elige otra ubicación con: gepa setup data-dir <ruta>.")]
    return [Finding("data-dir", "ok", "data-dir-ok", f"Carpeta de datos lista en {path}.")]


def _check_folders(settings: Settings) -> list[Finding]:
    """Whether the project's folders for templates and adapters were designated; a setup from before they existed only gets a warning."""
    folders = resolved_folders(settings)
    where = f"plantillas en {folders['templates']} y adaptadores propios en {folders['adapters']}"
    if settings.folders is None:
        return [Finding("folders", "warning", "folders-undesignated", f"Las carpetas del proyecto nunca se designaron; mientras tanto se usan las propuestas: {where}.",
                        "Desígnalas con: gepa setup folders (acepta las propuestas, o indica otras con --templates y --adapters); también escribe el .gitignore.")]
    return [Finding("folders", "ok", "folders-ok", f"Carpetas del proyecto: {where}.")]


def _credential_step(connection: Connection, encrypted_store: bool) -> str:
    variable = connection.api_key_env or ("OPENROUTER_API_KEY" if connection.provider == "openrouter" else f"GEPA_KEY_{connection.id.upper()}")
    declare = f"Define la variable de entorno {variable} con la API key (y declárala con --api-key-env {variable} si aún no lo está)"
    if not encrypted_store:
        return f"{declare}; en este sistema no hay almacén cifrado de claves, así que la variable es la única vía."
    return f"{declare}, o guárdala cifrada con: gepa setup connection set-key {connection.id}."


def _provider_failure(connection: Connection, error: ProviderError, *, stage: str, encrypted_store: bool) -> Finding:
    component = f"connection:{connection.id}"
    code = getattr(error, "code", "provider-error")
    if code in ("unreachable", "timeout"):
        return Finding(component, "error", "provider-unreachable", f"No se pudo contactar con el proveedor de '{connection.id}' al {stage}: {error}", f"Comprueba que el servidor de modelos esté en marcha y responda en {connection.url}; corrige la URL con: gepa setup connection add --id {connection.id} ... --url <nueva-url>.")
    if code == "credential-rejected":
        return Finding(component, "error", "credential-rejected", f"El proveedor de '{connection.id}' rechazó la credencial al {stage}: {error}", _credential_step(connection, encrypted_store))
    if code == "credential-missing":
        return Finding(component, "error", "credential-missing", f"La conexión '{connection.id}' necesita una credencial: {error}", _credential_step(connection, encrypted_store))
    if stage == "generar una respuesta":
        return Finding(component, "error", "inference-failed", f"El modelo '{connection.model}' de '{connection.id}' aparece en el catálogo pero no generó una respuesta: {error}", f"Comprueba que el modelo esté cargado y acepte chat/completions en {connection.url}; revisa los registros del servidor y repite 'gepa setup check'.")
    return Finding(component, "error", "provider-error", f"El proveedor de '{connection.id}' devolvió un error al {stage}: {error}", f"Revisa el servidor en {connection.url} y sus registros; repite 'gepa setup check' cuando responda.")


def _check_connection(connection: Connection, probes: Probes, *, run_inference: bool) -> Finding:
    component = f"connection:{connection.id}"
    needs_key = connection.provider == "openrouter" or bool(connection.api_key_env)
    if needs_key and not probes.has_credential(connection):
        return Finding(component, "error", "credential-missing", f"La conexión '{connection.id}' ({connection.provider}) no tiene credencial configurada.",
                       _credential_step(connection, probes.encrypted_store()))
    try:
        models = probes.catalog(connection)
    except ProviderError as error:
        return _provider_failure(connection, error, stage="consultar el catálogo", encrypted_store=probes.encrypted_store())
    available = [m["id"] for m in models]
    if connection.model not in available:
        sample = ", ".join(available[:8]) or "ninguno"
        return Finding(component, "error", "model-missing", f"El modelo '{connection.model}' no existe en el catálogo de '{connection.id}'.", f"Modelos disponibles: {sample}. Cambia el modelo con: gepa setup connection add --id {connection.id} --provider {connection.provider} --url {connection.url} --model <id-del-catálogo>.")
    if not run_inference:
        return Finding(component, "ok", "connection-ok", f"Conexión '{connection.id}': modelo '{connection.model}' en el catálogo de {connection.url} (sin rol asignado; inferencia no probada).")
    try:
        result = probes.complete(connection)
    except ProviderError as error:
        return _provider_failure(connection, error, stage="generar una respuesta", encrypted_store=probes.encrypted_store())
    latency = result.get("latencyMs")
    detail = f" en {latency} ms" if isinstance(latency, (int, float)) else ""
    return Finding(component, "ok", "connection-ok", f"Conexión '{connection.id}': modelo '{connection.model}' respondió a una ejecución pequeña{detail}.")


def _check_roles(settings: Settings) -> list[Finding]:
    findings = []
    for role in ROLES:
        target = settings.roles.get(role)
        if not target:
            findings.append(Finding(f"role:{role}", "warning", "role-unassigned", f"El rol '{role}' no tiene conexión asignada.", f"Asigna una con: gepa setup role {role} <id-de-conexión>."))
        elif settings.connection(target) is None:
            findings.append(Finding(f"role:{role}", "error", "role-dangling", f"El rol '{role}' apunta a la conexión '{target}', que no existe.", f"Asigna una conexión existente con: gepa setup role {role} <id-de-conexión>."))
        else:
            findings.append(Finding(f"role:{role}", "ok", "role-ok", f"Rol '{role}' usa la conexión '{target}'."))
    return findings


def run_doctor(settings: Settings, probes: Probes) -> dict[str, Any]:
    findings: list[Finding] = []
    findings += _check_engine(probes)
    findings += _check_dependencies(settings, probes)
    findings += _check_data_dir(settings)
    findings += _check_folders(settings)
    assigned = set(settings.roles.values())
    if settings.connections:
        findings += [_check_connection(c, probes, run_inference=c.id in assigned) for c in settings.connections]
    else:
        findings.append(Finding("connections", "warning", "no-connections", "No hay conexiones a modelos configuradas.", "Añade una con: gepa setup connection add --id <id> --provider local|openrouter --model <modelo> [--url <url-/v1>] [--api-key-env <VARIABLE>]."))
    findings += _check_roles(settings)
    errors = sum(1 for f in findings if f.status == "error")
    warnings = sum(1 for f in findings if f.status == "warning")
    healthy = {f.component for f in findings if f.status == "ok"}
    ready = errors == 0 and all(f"role:{role}" in healthy and f"connection:{settings.roles.get(role)}" in healthy for role in ROLES)
    return {
        "ok": errors == 0,
        "ready": ready,
        "home": str(settings.home),
        "configPath": str(settings.config_path),
        "dataDir": str(settings.data_dir),
        "python": sys.executable,
        "platform": platform.platform(),
        "summary": {"errors": errors, "warnings": warnings, "checks": len(findings)},
        "findings": [asdict(f) for f in findings],
    }


def render_report(report: dict[str, Any]) -> str:
    icon = {"ok": "OK ", "warning": "AVISO", "error": "ERROR"}
    if report["ready"]:
        verdict = "listo para optimizar"
    elif report["ok"]:
        verdict = "instalado; falta asignar modelos a los tres roles"
    else:
        verdict = "faltan cosas"
    lines = [f"GEPA setup check — {verdict}", f"Configuración: {report['configPath']}", f"Datos: {report['dataDir']}", f"Python: {report['python']}", ""]
    for item in report["findings"]:
        lines.append(f"[{icon[item['status']]}] {item['component']} · {item['code']}: {item['message']}")
        if item["nextStep"]:
            lines.append(f"        Siguiente paso: {item['nextStep']}")
    summary = report["summary"]
    lines.append("")
    lines.append(f"{summary['checks']} comprobaciones, {summary['errors']} errores, {summary['warnings']} avisos.")
    return "\n".join(lines)
