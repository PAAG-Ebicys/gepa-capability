# Adaptador para una tarea nueva

Un **adaptador propio** hace falta cuando los adaptadores incluidos no pueden
ejecutar o puntuar la tarea: ejecutar código, usar un navegador o una API, otro
sandbox u otra forma de evaluar. Es una carpeta con `adapter.json` (la
declaración de la tarea) y `adapter.py` (cómo se ejecuta y se puntúa un caso).
El motor conserva el resto: la superficie mutable del artefacto (`SKILL.md`, o
las instrucciones y criterios de una política), GEPA, las particiones, los
límites, la evidencia y la reflexión.

## Operaciones

| Acción | Tool MCP | CLI |
| --- | --- | --- |
| Copiar el ejemplo | `gepa_adapter_init {path, example?}` | `gepa adapter init <carpeta> --json` |
| Comprobar | `gepa_adapter_check {path}` | `gepa adapter check <carpeta> --json` |

Hay dos ejemplos (`--example` o `example`); copia el más cercano a la tarea:

- `python-tests` (por defecto): una Skill escribe una función de Python y cada
  caso se puntúa con pruebas en un subproceso. Muestra `check` y `task.run`.
- `choice-costs`: una política JEV cuyo decisor elige una opción, y cada error
  puntúa según la matriz de costes `costs.json`. Muestra un recurso propio.

## Procedimiento

1. **Copiar un ejemplo** en una carpeta nueva dentro de la carpeta de
   adaptadores del proyecto, con el nombre del adaptador: la da
   `gepa setup show` en `folders.adapters` (por defecto `gepa/adaptadores/`) y
   la designa setup-gepa. Listo cuando la carpeta tiene `adapter.json`,
   `adapter.py` y los recursos del ejemplo.
2. **Declarar la tarea** en `adapter.json` con la persona (ver «adapter.json»).
   Listo cuando cada clave describe la tarea nueva, `name` es propio y
   `artifact` es el tipo del original.
3. **Escribir `adapter.py`** (ver «adapter.py» y «Resultados»). Listo cuando
   `execute`, `evaluate` y, si la tarea los necesita, `check_case` y `check`
   están escritos para la tarea, y `execute` trabaja solo con `task.input`.
4. **Comprobar** y resolver cada hallazgo con `status: "error"` según su
   `resolvableBy`:
   - `agent`: da tú el `nextStep` (instalar un módulo o un comando, corregir
     `adapter.py`) con los permisos del anfitrión.
   - `person`: pide a la persona el dato o la decisión del `nextStep`. Una
     credencial se define como variable de entorno donde se ejecuta gepa; su
     valor se queda fuera de la conversación.

   Explica cada `warning` a la persona (por ejemplo, lo que el aislamiento de
   la tarea no cubre). Listo cuando `ready` es `true` y la persona conoce cada
   aviso.
5. **Preparar** con `"adapter": {"path": "<carpeta>"}` en la solicitud, sin
   `executor` ni `evaluator`, y sigue el paso **Preparar** de la skill. Cómo
   se escribe la ruta de `<carpeta>`: «Archivos intermedios» en
   [SKILL.md](SKILL.md). Listo cuando hay un `draftId`.

## adapter.json

Todas las claves son obligatorias salvo `requirements`; una clave desconocida
da `invalid-adapter`.

| Clave | Qué declara |
| --- | --- |
| `format`, `name`, `version`, `description` | `"gepa-adapter-v1"`, un identificador propio (minúsculas y guiones), la versión y la tarea en una frase |
| `artifact` | `"skill"` o `"jev-policy"`: el tipo del original |
| `input` | La **entrada**: `description` (qué trae el campo `input` de un caso, lo único que ve el ejecutor), `evaluatorFields` (los campos que solo lee el evaluador, como `expected`) y `optional` (los de `evaluatorFields` que un caso puede omitir) |
| `resources` | Los **recursos** del caso: `description` y `files` (archivos de la carpeta del adaptador que usa) |
| `tools` | Las **herramientas** del ejecutor: `[{"name", "description"}]`, vacía si no usa ninguna |
| `environment` | El **entorno**: `description`, `network` y `codeExecution` (true o false) e `isolation` (qué aísla y qué no) |
| `output` | La **salida observable**: `description` |
| `evaluation` | La **evaluación**: `name`, `version`, `primaryMetric` (score por caso de 0 a 1, más alto es mejor), `description` y `rule` (cómo se forma el score, con los requisitos obligatorios) |
| `roles` | `executor` y, si `evaluate` consulta un juez, `judge`; cada uno con `maxTokens` por llamada |
| `requirements` | `[{"kind", "name", "purpose", "install"?}]`, con `kind` `python` (módulo), `command` (comando en el PATH) o `credential` (variable de entorno). Un navegador, una API o un sandbox se comprueban en `check` |

Todo lo que ve el ejecutor va dentro de `input` (texto u objeto JSON): el motor
reconoce por `input` un caso repetido o ya observado, y dos casos con el mismo
`input` y distintos campos del evaluador son un duplicado en conflicto. Los
nombres del motor (`id`, `split`, `source`, `group`, `notes`, `output`, `score`
y demás campos de la evidencia) están reservados.

## adapter.py

| Función | Qué hace |
| --- | --- |
| `execute(task)` | Aplica el candidato a un caso y devuelve lo observado |
| `evaluate(task, observation)` | Puntúa lo que devolvió `execute` |
| `check_case(case)` (opcional) | Devuelve `None` o el problema del caso; corre al preparar |
| `check(environment)` (opcional) | Comprueba antes de llamar a modelos lo que no cubren los `requirements` y devuelve hallazgos `{"status", "code", "message", "nextStep", "resolvableBy", "requirement"?}` |

`adapter.py` es un solo módulo: sus otros archivos (datos, scripts que
ejecuta) se leen desde `task.adapter_dir`, no se importan.

`task` ofrece:

- `artifact` (la Skill candidata `{"files": {...}}` o la política candidata),
  `artifact_path` (su copia en el espacio aislado), `input` (la entrada del
  caso), `workspace` (una carpeta nueva por caso), `adapter_dir` (la copia
  congelada del adaptador) y `python` (el intérprete del motor).
- `case`: el caso completo, solo en `evaluate`.
- `model(role, messages)`: `{"text", "truncated", ...}`. `executor` en
  `execute` y `judge` en `evaluate`, con los `maxTokens` declarados. Una
  respuesta cortada llega con `truncated: true`: es un resultado del caso.
- `judge(criteria, work)`: el juez del motor sobre una rúbrica, con hasta 3
  reintentos; devuelve `score`, razones, `submetrics` y `feedback`.
- `run(args, timeout=..., input=..., env=...)`: un comando en el espacio aislado,
  con salida UTF-8, sin las variables de credenciales del entorno; `env` añade
  las que el comando necesite.
- `credential(name)`: el valor de una credencial declarada.
- `infrastructure_error(message)`: el error que se lanza cuando falla el entorno
  de la tarea (`raise task.infrastructure_error("...")`).
- `note(message)`: una nota en los pasos del caso.

`environment` ofrece `run`, `workspace`, `adapter_dir`, `python` y
`credential(name)` (su valor, o `None` si falta).

## Resultados

- `execute` devuelve un objeto JSON; su `output` (texto) es la salida del caso.
- `evaluate` devuelve `score` (número finito de 0 a 1) y `feedback` (el
  diagnóstico del que aprende la reflexión), y opcionalmente `submetrics`
  (nombre → true/false o número), `requirementsMet`, `taskSuccess` y `error`.
  Con `requirementsMet: false` el score es 0: un requisito obligatorio
  incumplido no se compensa.
- Si un caso puede marcar una comprobación como obligatoria, nombra la marca
  en `input.description` y su efecto en `evaluation.rule`, como
  `python-tests`: ahí la busca el agente que prepara los casos
  («Comprobaciones obligatorias» en [SKILL.md](SKILL.md)).
- El motor guarda por caso `observation`, `effects` (archivos creados o
  modificados en el espacio aislado) y `steps` (llamadas a modelos, comandos y
  notas, en orden). `evaluate` recibe siempre lo que devolvió `execute`
  completo; si su JSON pasa de 100 000 caracteres, la evidencia guarda solo su
  `output`, su tamaño y su hash (`omitted: true`). El valor de cada credencial
  declarada (de 4 caracteres o más) queda oculto en la evidencia y en los
  mensajes de error, también en los de `check`.
- Si el caso ejecuta código del candidato, ejecútalo en un proceso distinto del
  que compara con lo esperado, y deja lo esperado fuera de su alcance, como hace
  `python-tests`: así ese código no puede cambiar su propia puntuación.
- Un fallo de la tarea es un resultado con su score y su feedback. Un resultado
  fuera del contrato, un error del adaptador, un requisito ausente, un fallo de
  un modelo o del entorno detienen la evaluación sin puntuar al candidato,
  también si el adaptador captura la excepción.

## Congelado

La preparación guarda una copia del adaptador, identificada por el SHA-256 de su
carpeta, y los trabajos usan solo esa copia. Si la carpeta cambia después de
preparar, la previsualización, la aprobación y la creación de un trabajo dan
`adapter-changed`: prepara y aprueba de nuevo para usar los cambios. Si la
carpeta ya no existe, sigue valiendo la copia aprobada. Si la copia congelada
cambia mientras corre un caso, la evaluación se detiene con `adapter-changed`.

## Qué revisar con la persona

- En el resumen: `adapter` (nombre, versión, `sha256`, requisitos),
  `fixedContract.task` (entrada, recursos, herramientas, entorno, salida y
  modelos) y `evaluator` (`description` y `aggregation.rule`).
- En la previsualización: `output`, `score`, `submetrics`, `feedback`,
  `requirementsMet`, `observation`, `effects` y `steps`. Pregunta si cada score
  coincide con el criterio de la persona: un desacuerdo revela un `evaluate`
  mal orientado, que se corrige en `adapter.py` antes de aprobar.

## Códigos

| code | Qué significa | Qué hacer |
| --- | --- | --- |
| `invalid-adapter` | `adapter.json` o `adapter.py` no cumplen el contrato, o el adaptador es para otro tipo de artefacto | Corregir según el mensaje |
| `adapter-unreadable` | La carpeta no existe o no se puede leer | Corregir la ruta de `adapter.path` |
| `adapter-requirement-missing` | Falta un requisito declarado o falla `check` | Repetir el paso **Comprobar** |
| `adapter-invalid-result` | `execute` o `evaluate` devolvieron algo fuera de «Resultados» | Corregir `adapter.py` y preparar de nuevo |
| `adapter-error` | El código del adaptador lanzó un error o usó el contrato mal (leer `task.case` en `execute`, un rol no declarado) | Corregir `adapter.py` según el mensaje |
| `infrastructure-error` | Falló el entorno de la tarea (un comando que no arranca, una API caída) | Resolver el entorno y repetir |
| `adapter-changed` | La carpeta cambió desde la preparación, o la copia congelada cambió durante un caso | Preparar, previsualizar y aprobar de nuevo, o restaurar la versión preparada |
| `adapter-missing` | La copia congelada ya no está en la carpeta de datos | Preparar y aprobar de nuevo |
| `adapter-target-not-empty` / `adapter-init-failed` | La carpeta de `init` tiene contenido, o no se pudo escribir | Elegir otra carpeta nueva |
