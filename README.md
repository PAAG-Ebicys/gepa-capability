# Capacidad GEPA

Optimiza Skills y políticas de decisión JEV (las instrucciones y criterios con
los que un modelo elige una opción entre varias) desde tu agente: Claude Code,
Codex, OpenCode o Pi. Es un paquete Python, `gepa-capability`, con la CLI
`gepa`, un servidor MCP (protocolo con el que un agente expone tools nativas) y
cuatro skills que enseñan al agente a usarlo. Guía completa:
[docs/install.md](docs/install.md).

Requisitos: git, Python 3.12 o posterior, conexión a internet (pip descarga las
dependencias de PyPI) y acceso a modelos: un servidor local compatible con la
API de OpenAI (LM Studio, Ollama, llama.cpp, MLX…) o una cuenta de OpenRouter.
El repositorio es privado: tu cuenta de GitHub necesita una invitación.

## Instalar con tu agente

Abre el agente en la carpeta raíz de tu proyecto y pídele:

> Instala la capacidad GEPA desde https://github.com/PAAG-Ebicys/gepa-capability
> en este proyecto. El repositorio es privado: lee su README con
> `gh repo view PAAG-Ebicys/gepa-capability` o clonándolo con git.

## Pasos para el agente

Sigue los pasos en orden. Si uno falla, detente y díselo a la persona con el
error exacto.

1. **Comprueba el acceso** al repositorio:

   ```bash
   git ls-remote https://github.com/PAAG-Ebicys/gepa-capability
   ```

   Si responde `Repository not found` o pide credenciales, la cuenta de git de
   esta máquina no tiene acceso. Pide a la persona que acepte la invitación al
   repositorio y que inicie sesión, por ejemplo con `gh auth login` y después
   `gh auth setup-git`, o en la ventana de Git Credential Manager. No sigas
   hasta que el comando responda.

2. **Instala el motor una vez por máquina, en un entorno que dure.** Nunca con
   `uvx` ni en una carpeta temporal: el paso 3 guarda la ruta de ese Python en
   cada skill y en el registro MCP, y un entorno temporal desaparece. Si la
   persona prefiere otra carpeta para el entorno, usa la suya.

   Con [uv](https://docs.astral.sh/uv/):

   ```bash
   uv tool install "git+https://github.com/PAAG-Ebicys/gepa-capability@v0.1.0" --python 3.12
   ```

   Sin uv, en Windows (PowerShell):

   ```powershell
   py -3.12 -m venv $HOME\gepa-venv
   & $HOME\gepa-venv\Scripts\python.exe -m pip install "git+https://github.com/PAAG-Ebicys/gepa-capability@v0.1.0"
   ```

   Sin uv, en macOS o Linux:

   ```bash
   python3.12 -m venv ~/gepa-venv
   ~/gepa-venv/bin/python -m pip install "git+https://github.com/PAAG-Ebicys/gepa-capability@v0.1.0"
   ```

3. **Instala las skills en el proyecto**, desde su carpeta raíz, con el `gepa`
   del paso 2:

   ```bash
   gepa setup install --host claude-code
   ```

   Si `gepa` no está en el PATH, usa su ruta completa: la carpeta que indica
   `uv tool dir --bin`, `$HOME\gepa-venv\Scripts\gepa.exe` (Windows) o
   `~/gepa-venv/bin/gepa` (macOS o Linux). En PowerShell, antepón `&` a una
   ruta entre comillas. Para otro agente cambia `--host`: `codex`, `opencode`,
   `pi` o `generic`. El comando copia las cuatro skills donde el agente las
   descubre y, si el agente lo admite, registra el servidor MCP `gepa`.

4. **Comprueba el motor** con `gepa setup check`. Un `ready: false` con
   `no-connections` o `role-unassigned` es lo esperado: faltan los modelos.

5. **Termina aquí.** Dile a la persona que reinicie el agente desde la carpeta
   raíz del proyecto, porque las skills se cargan al arrancar; que apruebe el
   servidor MCP `gepa` si el agente lo pregunta; y que después pida «configura
   GEPA». La skill `setup-gepa` guía la conexión de los modelos.

## Actualizar a otra versión

El sufijo `@v0.1.0` fija la versión instalada. Para cambiarla, reinstala con el
tag nuevo y repite el paso 3 en cada proyecto, que copia las skills nuevas:

```bash
uv tool install --force "git+https://github.com/PAAG-Ebicys/gepa-capability@<tag>" --python 3.12
```

Con un venv: `python -m pip install --upgrade "git+https://github.com/PAAG-Ebicys/gepa-capability@<tag>"`
con el Python de ese venv.
