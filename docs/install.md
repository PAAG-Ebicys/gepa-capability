# Instalar GEPA Capability

GEPA Capability tiene tres piezas: el paquete Python `gepa-capability` (motor y CLI `gepa`), cuatro skills para el agente y un servidor MCP opcional. Instala el motor **una vez en un entorno persistente** y después copia las skills a cada proyecto donde lo usarás. El [README](../README.md) explica qué hace cada pieza y la [guía de uso](flujo.md) sigue un experimento completo.

## Requisitos

- Python 3.12 o posterior y git.
- Acceso al repositorio de GitHub; si es privado para tu cuenta, acepta la invitación e inicia sesión con git o GitHub CLI.
- Red durante la instalación para descargar las dependencias.
- Para ejecutar casos o buscar mejoras, acceso a los modelos que elijas mediante un servidor local compatible con OpenAI API u OpenRouter. **No necesitas escoger nombres de modelos para instalar las skills.**

Comprueba primero el acceso al código:

```bash
git ls-remote https://github.com/PAAG-Ebicys/gepa-capability
```

Si recibes `Repository not found` o una petición de credenciales, resuelve el acceso antes de instalar. Con GitHub CLI, `gh auth login` seguido de `gh auth setup-git` suele configurar git; también puedes usar Git Credential Manager.

## 1. Instalar el motor

Con [uv](https://docs.astral.sh/uv/), instala el código actual de la rama principal en el entorno persistente que uv administra:

```bash
uv tool install "git+https://github.com/PAAG-Ebicys/gepa-capability@main" --python 3.12
```

Si `gepa` no aparece en el PATH, ejecuta `uv tool update-shell` y abre otra terminal, o localiza la carpeta de binarios con `uv tool dir --bin`.

Sin uv, crea un entorno virtual en **una carpeta permanente que elijas**. En Windows PowerShell:

```powershell
py -3.12 -m venv "<ruta-del-venv>"
& "<ruta-del-venv>\Scripts\python.exe" -m pip install "git+https://github.com/PAAG-Ebicys/gepa-capability@main"
```

En macOS o Linux:

```bash
python3.12 -m venv "<ruta-del-venv>"
"<ruta-del-venv>/bin/python" -m pip install "git+https://github.com/PAAG-Ebicys/gepa-capability@main"
```

Sustituye `<ruta-del-venv>` por una ruta real: no es texto para copiar literalmente. `@main` sigue la rama principal y puede cambiar; para reproducir una instalación sustituye `main` por el SHA de un commit verificado. El tag `@v0.1.0` fija la versión anterior y puede no incluir las funciones descritas en el recorrido actual. Si recibiste una wheel `gepa_capability-<versión>-py3-none-any.whl`, puedes pasar su ruta a `uv tool install` o a `python -m pip install` en lugar de la URL. El paquete no se instala con `pip install gepa-capability` desde PyPI. Evita `uvx`: su entorno temporal puede desaparecer, mientras que las skills y MCP guardan la ruta del Python instalado.

## 2. Instalar las skills en un proyecto

Abre una terminal en la **raíz del proyecto**. Ejecuta el `gepa` recién instalado:

```bash
gepa setup install --host codex
```

Usa `--host claude-code`, `opencode`, `pi` o `generic` si corresponde. Con un venv, llama a su `Scripts/gepa.exe` (Windows) o `bin/gepa` (macOS y Linux). En PowerShell, antepón `&` a una ruta entre comillas.

| Anfitrión | Carpeta de skills en el proyecto | Registro MCP |
| --- | --- | --- |
| `codex` | `.agents/skills` | Imprime el comando `codex mcp add …` |
| `claude-code` | `.claude/skills` | Escribe `.mcp.json` |
| `opencode` | `.agents/skills` | Escribe `opencode.json` |
| `pi` | `.agents/skills` | Usa CLI, sin MCP |
| `generic` | `.agents/skills` | Usa CLI; imprime indicaciones de MCP si aplica |

El comando copia `setup-gepa`, `prepare-gepa-experiment`, `optimize-with-gepa` y `review-gepa-results`. También genera un `engine.md` con la ruta del intérprete y la configuración del motor; por eso el agente puede usar la CLI aunque `gepa` no esté en su PATH. Con `--scope user` puedes instalar las skills para todos tus proyectos, pero configura explícitamente `GEPA_HOME` o `--home` si quieres que el registro MCP global apunte a una misma configuración. La opción por defecto es `project`.

## 3. Comprobar y configurar modelos cuando los necesites

```bash
gepa setup check --json
```

Este diagnóstico distingue motor instalado, conexiones y roles. El `ready` **global** todavía exige conexiones sanas para `executor`, `reflection` y `judge`. Por eso `ready: false` por un rol pendiente **no significa que la instalación del paquete haya fallado**; un trabajo puede usar solo los roles que requiera su adaptador y elegirlos en `models`. Recarga el agente para que descubra las skills y pide «configura GEPA» cuando quieras ejecutar una previsualización o una búsqueda. Aprueba el servidor MCP del proyecto si el agente lo solicita; la CLI sigue disponible sin MCP.

Una conexión a servidor local utiliza la URL `/v1` y el identificador del modelo que sirve ese endpoint:

```bash
gepa setup connection add --id local-a --provider local --url http://127.0.0.1:1234/v1 --model "<id-del-modelo>"
```

Puedes crear varias conexiones y asignar roles generales con `gepa setup role executor <id-conexión>`, `gepa setup role reflection <id-conexión>` y, cuando una rúbrica lo requiera, `gepa setup role judge <id-conexión>`. Una corrida también puede elegir conexiones por rol en su campo `models` sin fijar esos nombres en la skill. Una tarea puede usar un adaptador propio para controlar su ejecución y evaluación; el adaptador declara los roles y dependencias que necesita.

Para OpenRouter, guarda la clave en una variable de entorno, nunca en un argumento o archivo de proyecto:

```bash
gepa setup connection add --id remoto --provider openrouter --model "<org/modelo>" --api-key-env OPENROUTER_API_KEY
```

El entorno del proceso que ejecute GEPA debe tener esa variable. En Windows también existe `gepa setup connection set-key remoto`, que pide la clave por stdin y la guarda cifrada para ese usuario; en macOS y Linux usa la variable de entorno. Al preparar una tarea que emplea un adaptador propio, `gepa adapter check` comprueba sus dependencias.

## Ubicación de datos

| Elemento | Opción | Valor inicial |
| --- | --- | --- |
| Configuración | `GEPA_HOME` o `gepa --home <carpeta>` | `.gepa` del proyecto actual |
| Datasets y trabajos | `GEPA_DATA_DIR` o `gepa setup data-dir <carpeta>` | `<GEPA_HOME>/data` |
| Solicitudes intermedias del agente | Sigue la configuración | `<GEPA_HOME>/solicitudes` |

`gepa setup install` fija la configuración correspondiente en `engine.md` y, cuando procede, en MCP. Ejecuta los comandos desde la raíz del proyecto para que todas las operaciones vean los mismos trabajos.

## Si algo falla

| Síntoma | Qué revisar |
| --- | --- |
| `Repository not found` | Acceso de la cuenta de git al repositorio |
| Python anterior a 3.12 | Crear el venv con Python 3.12+ |
| `gepa: command not found` | Usar la ruta de `gepa` del venv o configurar el PATH de uv |
| PowerShell dice `Token '-m' inesperado` | Anteponer `&` al ejecutable entre comillas |
| `no-connections` o `role-unassigned` | El motor está instalado; faltan conexiones o roles para la operación solicitada |
| `provider-unreachable` | Arrancar el servidor de modelos y revisar su URL `/v1` |
| `credential-missing` | Definir la variable indicada en el entorno donde corre el agente |
| `data-dir-unwritable` | Elegir una carpeta de datos con permisos de escritura |

La compatibilidad del comando de instalación y del registro MCP depende del anfitrión. La verificación documentada de esta versión cubrió Windows 10 y Claude Code; las rutas de otros anfitriones y macOS están disponibles, pero no cuentan con esa misma verificación. Si una skill no aparece tras la instalación, reinicia el agente desde la raíz del proyecto.
