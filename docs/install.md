# Instalar la capacidad GEPA

La capacidad GEPA optimiza Skills y políticas de decisión JEV (las
instrucciones y criterios con los que un modelo elige una opción entre varias)
desde tu agente. Tiene tres piezas:

- un **motor local**: un paquete Python (`gepa-capability`) que guarda los
  trabajos y ejecuta la búsqueda con GEPA;
- **cuatro skills** (`setup-gepa`, `prepare-gepa-experiment`, `optimize-with-gepa`
  y `review-gepa-results`) que enseñan al agente a usar el motor;
- dos formas de llamar al motor: la **CLI** `gepa` (comandos de terminal) y un
  servidor **MCP** (protocolo con el que un agente expone tools nativas).
  Las dos dan el mismo resultado.

El paquete no está publicado en PyPI (el repositorio público de paquetes
Python). Se distribuye de dos formas:

- como una **wheel**: un archivo instalable con nombre
  `gepa_capability-<versión>-py3-none-any.whl`;
- desde el repositorio privado de GitHub
  [`PAAG-Ebicys/gepa-capability`](https://github.com/PAAG-Ebicys/gepa-capability),
  que un agente puede instalar siguiendo su README. Necesita git y una cuenta
  de GitHub invitada al repositorio.

## Recorrido breve

Requisitos:

- Python 3.12 o posterior.
- Un agente con skills y terminal, por ejemplo Claude Code (ver
  [Anfitriones verificados](#anfitriones-verificados)).
- Acceso a modelos: un servidor local compatible con la API de OpenAI (la
  interfaz HTTP que usan esos servidores: LM Studio, Ollama, llama.cpp, MLX…) o
  una cuenta de OpenRouter.

### 1. Instalar el motor, una vez por máquina

Un **venv** es una carpeta con un Python propio y sus paquetes, separada del
resto del sistema. Crea uno e instala la wheel dentro.

Windows (PowerShell):

```powershell
py -3.12 -m venv $HOME\gepa-venv
& $HOME\gepa-venv\Scripts\python.exe -m pip install .\gepa_capability-0.1.0-py3-none-any.whl
```

macOS (Terminal; estos comandos siguen la documentación de Python y aún no se
verificaron en un Mac):

```bash
python3.12 -m venv ~/gepa-venv
~/gepa-venv/bin/python -m pip install ./gepa_capability-0.1.0-py3-none-any.whl
```

El Python del sistema de macOS (`/usr/bin/python3`) puede ser anterior a 3.12:
compruébalo con `python3 --version` e instala uno nuevo si hace falta (por
ejemplo `brew install python@3.12` o `uv python install 3.12`).

pip descarga de PyPI las dependencias (`gepa==0.1.4`, `httpx` y `openpyxl`),
así que este paso necesita conexión.

Con [uv](https://docs.astral.sh/uv/) instalado, un solo comando instala el motor
en un entorno propio y deja el comando `gepa` en la carpeta de herramientas de uv:

```bash
uv tool install ./gepa_capability-0.1.0-py3-none-any.whl --python 3.12
```

Si uv avisa de que esa carpeta no está en el PATH (la lista de carpetas donde
la terminal busca comandos), ejecuta `uv tool update-shell` y abre otra terminal.

Desde GitHub, en cualquiera de los comandos anteriores cambia la ruta de la
wheel por la dirección del repositorio con su versión, entre comillas:
`"git+https://github.com/PAAG-Ebicys/gepa-capability@v0.1.0"`. pip o uv
descargan el código con git y construyen la wheel ellos mismos, así que la
cuenta de git de la máquina necesita acceso al repositorio (compruébalo con
`git ls-remote https://github.com/PAAG-Ebicys/gepa-capability`). No uses `uvx`
para este paso: el paso 2 guarda la ruta del Python del motor, y el entorno
temporal de `uvx` puede desaparecer.

### 2. Instalar las skills en tu proyecto

En la carpeta raíz del proyecto, con el `gepa` del venv:

```powershell
& $HOME\gepa-venv\Scripts\gepa.exe setup install --host claude-code    # Windows
```

```bash
~/gepa-venv/bin/gepa setup install --host claude-code                  # macOS
```

Con uv basta `gepa setup install --host claude-code`. Para otro agente cambia
`--host` (ver [Anfitriones](#anfitriones-y-ámbito)). El comando copia las cuatro
skills donde el agente las descubre y, si el agente lo admite, registra el
servidor MCP `gepa`. Cada skill recibe un archivo `engine.md` con el comando
exacto del motor en tu máquina: el agente lo usa aunque `gepa` no esté en su PATH.

### 3. Pedirle al agente que configure GEPA

Reinicia o recarga el agente y pide «configura GEPA». La skill `setup-gepa`
comprueba el motor y te guía para conectar los modelos (siguiente sección).
Cuando `gepa setup check` responde `ready: true`, pide por ejemplo «optimiza
esta skill con estos ejemplos».

## Configurar los modelos

GEPA usa tres **roles**, cada uno con una conexión a un modelo:

| Rol | Qué hace |
| --- | --- |
| `executor` | Ejecuta el artefacto: responde como decisor JEV o sigue la Skill en cada caso |
| `reflection` | Lee los fallos de la búsqueda y propone candidatos nuevos |
| `judge` | Puntúa con una rúbrica cuando el evaluador lo pide (`rubric-judge`) |

Un servidor local se declara con su URL (la dirección web donde responde)
terminada en `/v1` y el identificador del modelo tal como aparece en su catálogo:

```bash
gepa setup connection add --id local --provider local --url http://127.0.0.1:1234/v1 --model <modelo>
gepa setup role executor local
gepa setup role reflection local
gepa setup role judge local
gepa setup check
```

Se aceptan `localhost`, direcciones IP privadas (las de tu red local, como
`192.168.1.20`) y el rango de Tailscale (`100.64.0.0/10`), siempre con la IP
escrita, no un nombre de equipo. Para OpenRouter, la API key (la credencial del
proveedor) va en una variable de entorno, nunca en un argumento ni en un archivo
del proyecto:

```bash
export OPENROUTER_API_KEY=...          # macOS; en PowerShell: $env:OPENROUTER_API_KEY = "..."
gepa setup connection add --id router --provider openrouter --model <org/modelo> --api-key-env OPENROUTER_API_KEY
```

En Windows también puedes guardarla cifrada para tu usuario con
`gepa setup connection set-key router` (la clave se escribe en la terminal, no en
argumentos). En macOS y Linux ese almacén no existe: usa la variable.

`gepa setup check` prueba cada rol con una inferencia pequeña (16 tokens). Su
código de salida es 0 solo con `ready: true`. Cada hallazgo trae un `code` y un
siguiente paso; la tabla completa está en [setup-gepa.md](setup-gepa.md).

## Configuración avanzada

| Qué | Cómo | Por defecto |
| --- | --- | --- |
| Carpeta de configuración | `GEPA_HOME` o `gepa --home <carpeta>` | `.gepa` en la carpeta actual |
| Carpeta de datos (trabajos, resultados) | `gepa setup data-dir <ruta>` o `GEPA_DATA_DIR` | `<configuración>/data` |
| Archivos intermedios del agente (solicitudes al motor, salidas que guarda) | Siguen a la carpeta de configuración | `<configuración>/solicitudes` |
| Ámbito de las skills | `gepa setup install --scope user` | `project` (la carpeta actual o `--target`) |
| Registro MCP | `--no-mcp` lo omite | Según el anfitrión |
| Dependencias de una tarea | `gepa setup dependency add --kind python\|command --name <nombre>` | Ninguna |
| Requisitos de un adaptador propio | `gepa adapter check` | Ver [new-adapter.md](new-adapter.md) |

`gepa setup install` fija en `engine.md` y en el registro MCP del proyecto
(`.mcp.json`, `opencode.json`) la carpeta de configuración que usará el agente:
la de `--home` o `GEPA_HOME` si los indicas, o si no la `.gepa` del proyecto.
Así la CLI y las tools MCP ven los mismos trabajos aunque el agente las lance
desde otra carpeta. Los comandos que imprime para un registro fuera del
proyecto (`codex mcp add`, que escribe en la configuración de usuario de Codex,
o `--scope user`) solo fijan una carpeta indicada con `--home` o `GEPA_HOME`:
sin ella, cada proyecto usa su propia `.gepa`. Este caso no se verificó en Codex.

`--scope user` copia las skills bajo la carpeta personal (`~/.claude/skills`
para Claude Code) y no lee `CLAUDE_CONFIG_DIR` (la variable que mueve la
configuración de Claude Code a otra carpeta). Si la tienes definida, Claude Code
no verá esas skills: usa el ámbito de proyecto.

### Anfitriones y ámbito

| `--host` | Skills (ámbito de proyecto) | MCP |
| --- | --- | --- |
| `claude-code` | `.claude/skills` | Escribe `.mcp.json` |
| `codex` | `.agents/skills` | Imprime el comando `codex mcp add gepa -- …` |
| `opencode` | `.agents/skills` | Escribe `opencode.json` |
| `pi` | `.agents/skills` | Sin MCP: la skill usa la CLI |
| `generic` | `.agents/skills` | Imprime el comando, si el agente admite MCP por stdio |

Cualquier agente que lea skills de `.agents/skills` y ejecute comandos puede usar
`--host generic`: las skills llaman a la CLI con el comando de `engine.md`.

## Recuperar errores de instalación

Errores observados al verificar la instalación en Windows:

| Síntoma | Causa | Qué hacer |
| --- | --- | --- |
| `ERROR: Package 'gepa-capability' requires a different Python: 3.11.2 not in '>=3.12'` | El venv usa Python 3.11 o anterior | Crear el venv con 3.12 o posterior (`py -3.12 -m venv …`, `python3.12 -m venv …`) |
| `El término 'gepa' no se reconoce como nombre de un cmdlet…` (o `command not found: gepa`) | `gepa` está dentro del venv, que no está en el PATH | Usar la ruta completa (`$HOME\gepa-venv\Scripts\gepa.exe`, `~/gepa-venv/bin/gepa`) o instalar con `uv tool install`. El agente usa `engine.md` |
| `warning: ...\uv\bin is not on your PATH` tras `uv tool install` | La carpeta de herramientas de uv no está en el PATH | `uv tool update-shell` y abrir otra terminal, o usar la ruta completa de `gepa` |
| `Token '-m' inesperado en la expresión o la instrucción.` | En PowerShell, una ruta entre comillas es texto, no un comando | Anteponer `&`: `& "C:\…\python.exe" -m gepa_engine …` (así lo escribe `engine.md`) |
| `gepa setup check` da `ready: false` con `no-connections` o `role-unassigned` | Motor instalado, sin modelos asignados | Añadir conexiones y asignar los tres roles (sección anterior) |
| `provider-unreachable` | El servidor de modelos no responde en esa URL | Arrancarlo o corregir la URL con `gepa setup connection add … --url` |
| `credential-missing` | La conexión necesita API key y no la encuentra | Definir la variable indicada en `nextStep` en el entorno donde corre el agente |
| `data-dir-unwritable` | La carpeta de datos no se puede escribir | `gepa setup data-dir <otra-ruta>` |

Si el agente no ve las skills, reinícialo desde la raíz del proyecto donde
ejecutaste `gepa setup install`. Si no ve las tools MCP, puede que tengas que
aprobar el servidor del proyecto la primera vez; la CLI da lo mismo mientras tanto.

## Anfitriones verificados

La verificación instala la wheel en un venv nuevo, fuera del repositorio, y
recorre setup, preparación y aprobación, búsqueda Skill y JEV, una búsqueda
cortada al matar su proceso y continuada desde el último estado guardado de
GEPA, consulta, revisión, exportación, plantilla y plantillas de ejemplo con modelos controlados (ver
[Verificar una instalación](#verificar-una-instalación)). Comprueba la
instalación y el recorrido, no una mejora de ningún artefacto.

| Sistema | Instalación o anfitrión | Estado |
| --- | --- | --- |
| Windows 10 | Wheel en un venv nuevo con Python 3.12 | Verificado: los 16 pasos del recorrido, con una búsqueda cortada y continuada y las plantillas de ejemplo desde la wheel |
| Windows 10 | `uv tool install` con Python 3.12 | Verificado: los 15 pasos del recorrido anteriores a las plantillas de ejemplo, con una búsqueda cortada y continuada |
| Windows 10 | Claude Code 2.1 | Verificado: descubre las cuatro skills y conecta el servidor MCP del proyecto; `gepa_setup_check` y la CLI desde `engine.md` (sin MCP y sin `gepa` en el PATH) dan la misma configuración |
| Windows 10 | Codex, OpenCode, Pi, otros | Ruta por CLI documentada; no verificada en el anfitrión |
| macOS | Todos | No verificado |

Que un anfitrión aparezca en `--host` no significa que esté verificado.

## Verificar una instalación

`scripts/verify_distribution.py` (solo usa la biblioteca estándar; puedes
copiarlo a otra máquina junto con la wheel) crea un venv nuevo, instala la wheel
y recorre todo con un servidor de modelos controlado en `127.0.0.1`:

```bash
python scripts/verify_distribution.py --wheel gepa_capability-0.1.0-py3-none-any.whl --python python3.12 --work <carpeta-nueva>
```

Escribe `verification-report.json` en la carpeta indicada, con un paso por
etapa. Los modelos controlados responden mejor cuando un candidato contiene la
palabra `MEJORADA`: que un candidato gane allí está guionizado.

## Construir la wheel

Desde una copia limpia del repositorio (sin carpeta `build/`, que puede arrastrar
archivos antiguos):

```bash
python -m pip wheel --no-deps -w dist .
```

La descripción de la wheel es esta guía. Antes de distribuirla, verifícala con
`--forbid <texto>` para cada ruta o servidor privado que no deba aparecer.

## Publicar una versión en GitHub

El repositorio de GitHub no comparte historia con este: guarda solo lo que
construye el paquete (`pyproject.toml`, `gepa_engine/` y esta guía) y su README,
que sale de `docs/github-readme.md`. Cada versión es un commit de este
repositorio exportado con `git archive`, con un tag anotado que cita su SHA.

1. Aquí, cambia `version` en `pyproject.toml` y el tag de las direcciones
   `@v…` en esta guía y en `docs/github-readme.md`; haz el commit.
2. Exporta ese commit a una copia del repositorio de GitHub (la primera vez,
   una carpeta nueva con `git init -b main` y
   `git remote add origin https://github.com/PAAG-Ebicys/gepa-capability.git`):

   ```bash
   git clone https://github.com/PAAG-Ebicys/gepa-capability <copia>
   git -C <copia> rm -rq --ignore-unmatch .
   git archive <commit> pyproject.toml gepa_engine docs/install.md docs/github-readme.md | tar -x -C <copia>
   mv <copia>/docs/github-readme.md <copia>/README.md
   ```

3. Busca en la copia rutas, nombres o servidores privados, como hace `--forbid`
   con la wheel.
4. Publica el commit y el tag:

   ```bash
   git -C <copia> add -A
   git -C <copia> commit -m "release: v<versión> desde <commit>"
   git -C <copia> tag -a v<versión> -m "Exportado de <commit>"
   git -C <copia> push origin main v<versión>
   ```

5. Comprueba la instalación desde GitHub en un venv nuevo:
   `python -m pip install "git+https://github.com/PAAG-Ebicys/gepa-capability@v<versión>"`.
