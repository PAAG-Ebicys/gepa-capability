"""Host-independent GEPA configuration: a small JSON file under ``GEPA_HOME`` that never holds credentials.

Credentials come from either an environment variable named per connection (``api_key_env``)
or the OS-bound :class:`~gepa_engine.providers.SecretStore` next to the config file.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping

from .files import open_new
from .providers import OPENROUTER_URL, ProviderError, SecretStore, validate_connection

CONFIG_FORMAT = "gepa-config-v1"
ROLES = ("executor", "reflection", "judge")
DEPENDENCY_KINDS = ("python", "command")
ENGINE_PACKAGE = "gepa"
ENGINE_VERSION = "0.1.4"
PROJECT_HOME = ".gepa"  # what default_home() resolves inside the project when GEPA_HOME is not set
# Where a project keeps the person's experiment templates and own adapters, relative to the project, until `gepa setup folders` designates others.
DEFAULT_FOLDERS = {"templates": "gepa/plantillas", "adapters": "gepa/adaptadores"}


class ConfigError(Exception):
    """User-facing configuration problem; never contains secrets."""


@dataclass(frozen=True)
class Connection:
    id: str
    name: str
    provider: str
    url: str
    model: str
    api_key_env: str | None = None

    def as_provider_dict(self) -> dict[str, str]:
        return {"id": self.id, "name": self.name, "provider": self.provider, "url": self.url, "model": self.model}


@dataclass(frozen=True)
class Dependency:
    kind: str
    name: str
    purpose: str = ""


@dataclass(frozen=True)
class Folders:
    """The designated folders of a project, as the configuration keeps them: relative to the project (posix) when inside it, absolute otherwise."""

    templates: str
    adapters: str


@dataclass(frozen=True)
class Settings:
    home: Path
    data_dir: Path
    connections: tuple[Connection, ...] = ()
    roles: Mapping[str, str] = field(default_factory=dict)
    dependencies: tuple[Dependency, ...] = ()
    folders: Folders | None = None  # None: never designated, and the defaults apply

    def __post_init__(self) -> None:
        object.__setattr__(self, "roles", MappingProxyType(dict(self.roles)))

    @property
    def config_path(self) -> Path:
        return self.home / "config.json"

    @property
    def secrets_path(self) -> Path:
        return self.home / "secrets.json"

    def connection(self, identifier: str) -> Connection | None:
        return next((c for c in self.connections if c.id == identifier), None)


def default_home(environ: Mapping[str, str] | None = None) -> Path:
    """``GEPA_HOME`` or ``./.gepa``: configuration stays inside the project unless the user says otherwise."""
    environ = os.environ if environ is None else environ
    return Path(environ.get("GEPA_HOME") or (Path.cwd() / ".gepa")).expanduser().resolve()


def default_data_dir(home: Path, environ: Mapping[str, str] | None = None) -> Path:
    """``GEPA_DATA_DIR`` (shared with the previous app) wins over ``<home>/data``."""
    environ = os.environ if environ is None else environ
    return Path(environ.get("GEPA_DATA_DIR") or (home / "data")).expanduser().resolve()


def default_settings(home: Path) -> Settings:
    home = Path(home).expanduser().resolve()
    return Settings(home=home, data_dir=default_data_dir(home))


def _string(document: Mapping[str, Any], key: str, *, required: bool = True) -> str:
    value = document.get(key)
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise ConfigError(f"El campo '{key}' de la configuración es inválido.")
    return value.strip()


def _connection_from(document: Mapping[str, Any]) -> Connection:
    if not isinstance(document, Mapping):
        raise ConfigError("Cada conexión debe ser un objeto.")
    env = _string(document, "apiKeyEnv", required=False)
    return make_connection(
        identifier=_string(document, "id"),
        provider=_string(document, "provider"),
        model=_string(document, "model"),
        url=_string(document, "url", required=False),
        name=_string(document, "name", required=False),
        api_key_env=env or None,
    )


def make_connection(*, identifier: str, provider: str, model: str, url: str = "", name: str = "", api_key_env: str | None = None) -> Connection:
    """Validate through the same rules the gateway applies, so setup rejects what inference would reject."""
    if not identifier or any(ch in identifier for ch in " /\\\t\n") or len(identifier) > 100:
        raise ConfigError("El identificador de la conexión debe ser una palabra corta sin espacios ni barras.")
    if api_key_env is not None and (not api_key_env.isidentifier() or api_key_env != api_key_env.upper()):
        raise ConfigError("El nombre de la variable de entorno debe ser un identificador en mayúsculas, como OPENROUTER_API_KEY.")
    raw = {"id": identifier, "name": name, "provider": provider, "model": model, "url": url or (OPENROUTER_URL if provider == "openrouter" else "")}
    try:
        clean = validate_connection(raw)
    except ProviderError as error:
        raise ConfigError(str(error)) from None
    return Connection(id=clean["id"], name=clean["name"], provider=clean["provider"], url=clean["url"], model=clean["model"], api_key_env=api_key_env)


def make_dependency(*, kind: str, name: str, purpose: str = "") -> Dependency:
    if kind not in DEPENDENCY_KINDS:
        raise ConfigError("El tipo de dependencia debe ser 'python' o 'command'.")
    if not name or any(ch.isspace() for ch in name) or len(name) > 200:
        raise ConfigError("El nombre de la dependencia debe ser una palabra sin espacios.")
    return Dependency(kind=kind, name=name, purpose=purpose.strip())


def load_settings(home: Path | str | None = None) -> Settings:
    base = default_settings(default_home() if home is None else Path(home))
    path = base.config_path
    if not path.exists():
        return base
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as error:
        raise ConfigError(f"No se pudo leer {path}: archivo ilegible o JSON inválido.") from error
    if not isinstance(document, dict) or document.get("format") != CONFIG_FORMAT:
        raise ConfigError(f"{path} no tiene el formato {CONFIG_FORMAT}.")
    data_dir = document.get("dataDir")
    if not isinstance(data_dir, str) or not data_dir:
        raise ConfigError("La configuración no indica dataDir.")
    connections = tuple(_connection_from(item) for item in document.get("connections", []) or [])
    ids = [c.id for c in connections]
    if len(ids) != len(set(ids)):
        raise ConfigError("Hay conexiones con el mismo identificador.")
    roles_raw = document.get("roles", {}) or {}
    if not isinstance(roles_raw, dict) or any(key not in ROLES or not isinstance(value, str) for key, value in roles_raw.items()):
        raise ConfigError("Los roles deben ser executor, reflection o judge y apuntar a un identificador de conexión.")
    deps_raw = document.get("dependencies", []) or []
    if not isinstance(deps_raw, list):
        raise ConfigError("Las dependencias deben ser una lista.")
    dependencies = tuple(make_dependency(kind=str(d.get("kind", "")), name=str(d.get("name", "")), purpose=str(d.get("purpose", ""))) for d in deps_raw if isinstance(d, dict))
    folders_raw = document.get("folders")
    if folders_raw is not None and (not isinstance(folders_raw, dict) or set(folders_raw) != set(DEFAULT_FOLDERS)
                                    or not all(isinstance(value, str) and value.strip() for value in folders_raw.values())):
        raise ConfigError("'folders' debe indicar 'templates' y 'adapters' con la ruta de cada carpeta.")
    return Settings(home=base.home, data_dir=Path(data_dir).expanduser().resolve(), connections=connections, roles=dict(roles_raw), dependencies=dependencies,
                    folders=None if folders_raw is None else Folders(**folders_raw))


def to_document(settings: Settings) -> dict[str, Any]:
    return {
        "format": CONFIG_FORMAT,
        "dataDir": str(settings.data_dir),
        "connections": [
            {"id": c.id, "name": c.name, "provider": c.provider, "url": c.url, "model": c.model, **({"apiKeyEnv": c.api_key_env} if c.api_key_env else {})}
            for c in settings.connections
        ],
        "roles": dict(settings.roles),
        "dependencies": [asdict(d) for d in settings.dependencies],
        **({"folders": asdict(settings.folders)} if settings.folders else {}),
    }


def write_json_atomic(path: Path, document: Mapping[str, Any]) -> Path:
    """Write via a sibling temp file and ``os.replace`` so a crash never leaves a half-written file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary = open_new(path.parent, f".{path.name}-", ".tmp")
    with handle:
        json.dump(document, handle, ensure_ascii=False, indent=2)
        handle.write("\n")
    os.replace(temporary, path)
    return path


def save_settings(settings: Settings) -> Path:
    try:
        return write_json_atomic(settings.config_path, to_document(settings))
    except OSError as error:
        raise ConfigError(f"No se pudo escribir {settings.config_path}.") from error


def with_connection(settings: Settings, connection: Connection) -> Settings:
    others = tuple(c for c in settings.connections if c.id != connection.id)
    return replace(settings, connections=others + (connection,))


def without_connection(settings: Settings, identifier: str) -> Settings:
    if settings.connection(identifier) is None:
        raise ConfigError(f"No existe la conexión '{identifier}'.")
    roles = {role: target for role, target in settings.roles.items() if target != identifier}
    return replace(settings, connections=tuple(c for c in settings.connections if c.id != identifier), roles=roles)


def with_role(settings: Settings, role: str, identifier: str) -> Settings:
    if role not in ROLES:
        raise ConfigError(f"El rol debe ser uno de: {', '.join(ROLES)}.")
    if settings.connection(identifier) is None:
        raise ConfigError(f"No existe la conexión '{identifier}'; añádela primero con 'gepa setup connection add'.")
    return replace(settings, roles={**settings.roles, role: identifier})


def with_data_dir(settings: Settings, data_dir: Path | str) -> Settings:
    return replace(settings, data_dir=Path(data_dir).expanduser().resolve())


def with_dependency(settings: Settings, dependency: Dependency) -> Settings:
    others = tuple(d for d in settings.dependencies if (d.kind, d.name) != (dependency.kind, dependency.name))
    return replace(settings, dependencies=others + (dependency,))


def project_root(settings: Settings, cwd: Path | None = None) -> Path:
    """The project a configuration serves: the folder that holds a project's own ``.gepa``, so any subfolder finds the same one,
    or else the folder the engine runs from, so each project sharing an engine folder keeps its own."""
    return settings.home.parent if settings.home.name == PROJECT_HOME else Path(cwd or Path.cwd()).resolve()


def resolved_folders(settings: Settings, cwd: Path | None = None) -> dict[str, Path]:
    """The absolute templates and adapters folders: the designated ones, or the defaults inside the project."""
    project = project_root(settings, cwd)
    stored = asdict(settings.folders) if settings.folders else DEFAULT_FOLDERS
    return {role: (project / Path(value).expanduser()).resolve() for role, value in stored.items()}


def with_folders(settings: Settings, *, templates: Path | str, adapters: Path | str, cwd: Path | None = None) -> Settings:
    """Designate the folders; a relative path is relative to the project. Each must be apart from the project itself, the engine folder and the other one."""
    project = project_root(settings, cwd)
    chosen = {role: (project / Path(value).expanduser()).resolve() for role, value in (("templates", templates), ("adapters", adapters))}
    names = {"templates": "de plantillas", "adapters": "de adaptadores"}
    for role, path in chosen.items():
        if path == project or project.is_relative_to(path):
            raise ConfigError(f"La carpeta {names[role]} no puede ser el proyecto ({project}) ni contenerlo: elige una carpeta dentro o fuera de él.")
        if path.is_relative_to(settings.home) or settings.home.is_relative_to(path):
            raise ConfigError(f"La carpeta {names[role]} debe quedar fuera de la carpeta del motor ({settings.home}), que guarda credenciales y datos.")
    if chosen["templates"].is_relative_to(chosen["adapters"]) or chosen["adapters"].is_relative_to(chosen["templates"]):
        raise ConfigError("Las carpetas de plantillas y de adaptadores deben ser distintas y no estar una dentro de la otra.")
    stored = {role: path.relative_to(project).as_posix() if path.is_relative_to(project) else str(path) for role, path in chosen.items()}
    return replace(settings, folders=Folders(**stored))


def open_secret_store(settings: Settings) -> SecretStore:
    return SecretStore(settings.secrets_path)


def has_credential(connection: Connection, secrets: SecretStore | None, environ: Mapping[str, str] | None = None) -> bool:
    """True when some credential source exists. Never returns the value itself."""
    environ = os.environ if environ is None else environ
    if connection.api_key_env and environ.get(connection.api_key_env):
        return True
    if connection.provider == "openrouter" and environ.get("OPENROUTER_API_KEY"):
        return True
    return bool(secrets is not None and secrets.has(connection.id))


def credential_lookup(settings: Settings, secrets: SecretStore | None, environ: Mapping[str, str] | None = None) -> Callable[[str], str | None]:
    """Build the ``api_keys`` callable the gateway expects, honouring per-connection env variables."""
    environ = os.environ if environ is None else environ

    def lookup(identifier: str) -> str | None:
        connection = settings.connection(identifier)
        if connection is not None and connection.api_key_env and environ.get(connection.api_key_env):
            return environ[connection.api_key_env]
        return secrets.get(identifier) if secrets is not None else None

    return lookup


def public_view(settings: Settings, secrets: SecretStore | None, environ: Mapping[str, str] | None = None, cwd: Path | None = None) -> dict[str, Any]:
    """What ``gepa setup show`` prints: everything configurable, nothing secret."""
    folders = resolved_folders(settings, cwd)
    return {
        "home": str(settings.home),
        "configPath": str(settings.config_path),
        "dataDir": str(settings.data_dir),
        "engine": {"package": ENGINE_PACKAGE, "version": ENGINE_VERSION},
        "connections": [
            {"id": c.id, "name": c.name, "provider": c.provider, "url": c.url, "model": c.model, "apiKeyEnv": c.api_key_env, "hasKey": has_credential(c, secrets, environ)}
            for c in settings.connections
        ],
        "roles": {role: settings.roles.get(role) for role in ROLES},
        "dependencies": [asdict(d) for d in settings.dependencies],
        "folders": {"designated": settings.folders is not None, "project": str(project_root(settings, cwd)), **{role: str(path) for role, path in folders.items()}},
    }
