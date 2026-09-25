"""Place the portable GEPA skills where a host agent discovers them and wire MCP where the host supports it.

Hosts here are examples, not a closed list: any agent that reads Agent Skills from
``.agents/skills`` and can run a terminal command works through the ``generic`` entry.
"""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from typing import Any

from .config import PROJECT_HOME, write_json_atomic

MODULE_ARGS = ["-m", "gepa_engine"]
ENGINE_NOTE = "engine.md"  # written next to each installed SKILL.md: how to reach this installation's engine
SKILLS = ("setup-gepa", "prepare-gepa-experiment", "optimize-with-gepa", "review-gepa-results")

HOSTS: dict[str, dict[str, Any]] = {
    "claude-code": {"project": ".claude/skills", "user": ".claude/skills", "mcp": "mcp-json"},
    "codex": {"project": ".agents/skills", "user": ".agents/skills", "mcp": "manual"},
    "opencode": {"project": ".agents/skills", "user": ".config/opencode/skills", "mcp": "opencode-json"},
    "pi": {"project": ".agents/skills", "user": ".agents/skills", "mcp": "unsupported"},
    "generic": {"project": ".agents/skills", "user": ".agents/skills", "mcp": "manual"},
}


def skill_source(name: str = "setup-gepa") -> Path:
    return Path(__file__).resolve().parent / "skills" / name


def _copy_skill(destination_root: Path, name: str) -> list[str]:
    source = skill_source(name)
    target = destination_root / name
    target.mkdir(parents=True, exist_ok=True)
    written = []
    for item in source.rglob("*"):
        relative = item.relative_to(source)
        if item.is_dir():
            (target / relative).mkdir(parents=True, exist_ok=True)
            continue
        shutil.copyfile(item, target / relative)
        written.append(str(target / relative))
    return written


def _shell_line(args: list[str]) -> str:
    """A command line to paste in a shell: arguments with spaces (such as a user folder ``Ana María``) go in double quotes."""
    return " ".join(f'"{arg}"' if any(ch.isspace() for ch in arg) else arg for arg in args)


def _engine_note(command: list[str], home: Path | None) -> str:
    """``engine.md``: the engine command of this installation, for bash or zsh and for PowerShell."""
    python, rest = f'"{command[0]}"', _shell_line(command[1:])
    where = (f"La configuración es `{home}`: la misma que usan las tools MCP de esta instalación." if home else
             f"Sin `--home`, la configuración es `{PROJECT_HOME}` de la carpeta donde se ejecuta (o `GEPA_HOME`), como en las tools MCP.")
    return "\n".join([
        "# Cómo invocar el motor GEPA en esta máquina",
        "",
        "`gepa setup install` escribió este archivo para esta instalación. Usa el comando de abajo en lugar de `gepa`:",
        "llama al intérprete donde está instalado el motor, así que funciona aunque `gepa` no esté en el PATH del agente.",
        where,
        "",
        "bash o zsh:",
        "",
        f"    {python} {rest} setup check --json",
        "",
        "PowerShell (el operador `&` ejecuta la ruta entre comillas):",
        "",
        f"    & {python} {rest} setup check --json",
        "",
        "En las tablas de las skills, `gepa` abrevia este comando.",
        "",
    ])


def _merge_json(path: Path, top_key: str, entry_name: str, entry: dict[str, Any]) -> str:
    document: dict[str, Any] = {}
    if path.exists():
        try:
            document = json.loads(path.read_text(encoding="utf-8"))
        except ValueError as error:
            raise ValueError(f"{path} no es JSON válido; corrígelo o usa --no-mcp.") from error
        if not isinstance(document, dict):
            raise ValueError(f"{path} debe contener un objeto JSON.")
    section = document.get(top_key)
    if not isinstance(section, dict):
        section = {}
    document[top_key] = {**section, entry_name: entry}
    write_json_atomic(path, document)
    return str(path)


def install(*, host: str, scope: str = "project", target: Path | str | None = None, mcp: bool = True, user_home: Path | str | None = None, python: str | None = None,
            home: Path | str | None = None) -> dict[str, Any]:
    """Copy the skills and register MCP; ``home`` is the configuration folder given explicitly (``--home`` or ``GEPA_HOME``).

    The engine command each skill receives and the registration in the project's own file (``.mcp.json``, ``opencode.json``)
    name the same configuration, so the host reaches the same jobs by either door whatever folder it starts them from:
    ``home`` if given, else the project's ``.gepa``. A registration outside the project, and a user-scope install, pin only
    ``home``: without it each project keeps its own ``.gepa``.
    """
    if host not in HOSTS:
        raise ValueError(f"Anfitrión desconocido '{host}'. Opciones: {', '.join(sorted(HOSTS))}.")
    if scope not in ("project", "user"):
        raise ValueError("El ámbito debe ser 'project' o 'user'.")
    if scope == "user" and target is not None:
        raise ValueError("--target solo aplica al ámbito de proyecto; el ámbito de usuario instala en la carpeta personal.")
    python = python or sys.executable
    spec = HOSTS[host]
    if scope == "user":
        root = Path(user_home or Path.home()).expanduser().resolve()
    else:
        root = Path(target or Path.cwd()).expanduser().resolve()
    root.mkdir(parents=True, exist_ok=True)
    explicit = Path(home).expanduser().resolve() if home is not None else None
    engine_home = explicit or (root / PROJECT_HOME if scope == "project" else None)
    engine = [python, *MODULE_ARGS, *(["--home", str(engine_home)] if engine_home else [])]
    mcp_args = [*engine[1:], "mcp"]  # what a project's own registration file (.mcp.json, opencode.json) runs
    # A registration outside the project (`codex mcp add`, a user scope) serves every project: it pins only a folder chosen explicitly,
    # and a server started from the project resolves the same .gepa that engine.md names.
    command_line = _shell_line([python, *MODULE_ARGS, *(["--home", str(explicit)] if explicit else []), "mcp"])
    note = _engine_note(engine, engine_home)
    written = []
    for name in SKILLS:
        written += _copy_skill(root / spec[scope], name)
        path = root / spec[scope] / name / ENGINE_NOTE
        path.write_text(note, encoding="utf-8")
        written.append(str(path))
    instructions = [f"Skills {', '.join(SKILLS)} instaladas en {root / spec[scope]}. Reinicia o recarga el agente para que las descubra.",
                    f"Cada skill lleva {ENGINE_NOTE} con el comando del motor en esta máquina: {_shell_line(engine)}"]
    mcp_state = "skipped"
    if mcp:
        mode = spec["mcp"]
        if mode == "mcp-json" and scope == "project":
            written.append(_merge_json(root / ".mcp.json", "mcpServers", "gepa", {"command": python, "args": mcp_args}))
            mcp_state = "configured"
            instructions.append("Servidor MCP 'gepa' registrado en .mcp.json (ámbito de proyecto): las tools gepa_setup_*, gepa_dataset_* y gepa_job_* quedan disponibles.")
        elif mode == "mcp-json":
            mcp_state = "manual"
            instructions.append(f"Para el ámbito de usuario registra el servidor con: claude mcp add --scope user gepa -- {command_line}")
        elif mode == "opencode-json" and scope == "project":
            written.append(_merge_json(root / "opencode.json", "mcp", "gepa", {"type": "local", "command": [python, *mcp_args], "enabled": True}))
            mcp_state = "configured"
            instructions.append("Servidor MCP 'gepa' añadido a opencode.json (ámbito de proyecto).")
        elif mode == "opencode-json":
            mcp_state = "manual"
            instructions.append(f"Añade en la configuración de usuario de OpenCode una entrada mcp.gepa de tipo local con el comando: {command_line}")
        elif mode == "manual":
            mcp_state = "manual"
            if host == "codex":
                instructions.append(f"Registra el servidor MCP con: codex mcp add gepa -- {command_line}")
            else:
                instructions.append(f"Si el anfitrión admite MCP por stdio, registra el comando: {command_line}")
        else:
            mcp_state = "unsupported"
            instructions.append("Este anfitrión no documenta un cliente MCP integrado: la skill usa la CLI común ('gepa setup check') y obtiene el mismo resultado.")
    instructions.append("Comprueba el entorno con: gepa setup check")
    return {"host": host, "scope": scope, "root": str(root), "written": written, "mcp": mcp_state, "instructions": instructions,
            "engine": {"command": engine, "home": str(engine_home) if engine_home else None}}
