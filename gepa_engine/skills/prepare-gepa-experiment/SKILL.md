---
name: prepare-gepa-experiment
description: Prepara y aprueba los casos de un experimento GEPA para una política de decisión JEV o una Skill. Úsala cuando la persona quiera armar o revisar un dataset de optimización, aporte ejemplos conversados, pegados de Excel o en archivos Excel, CSV o JSON, quiera definir cómo se puntúa (comprobaciones o rúbrica con juez), la tarea necesite un adaptador propio (ejecutar código, un navegador o una API), quiera repetir una clase de prueba con una plantilla guardada o guardar una, o necesite aprobar una versión antes de optimizar.
license: MIT
compatibility: Requiere el motor comprobado con setup-gepa. MCP es opcional; la CLI `gepa` da el mismo resultado.
metadata:
  version: "0.1.0"
  engine: "gepa==0.1.4"
---

# prepare-gepa-experiment

Lleva ejemplos de la persona hasta un **dataset sellado**: casos validados, el
original ejecutado sobre algunos de ellos y una versión aprobada (`ds-…`) que
`optimize-with-gepa` puede usar. El motor importa, valida, divide y sella; tú
reúnes los casos con la persona, le presentas la evidencia y pides su aprobación.

## Operaciones

Cada tool del servidor MCP `gepa` y su comando CLI devuelven el mismo JSON.
Para la CLI, usa el comando de `engine.md` (en la carpeta de esta skill; `gepa`
lo abrevia en la tabla).

| Acción | Tool MCP | CLI |
| --- | --- | --- |
| Preparar | `gepa_dataset_prepare {requestPath}` | `gepa dataset prepare <prepare.json> --json` |
| Previsualizar el original | `gepa_dataset_preview {draftId, sample?, judge?}` | `gepa dataset preview <draftId> [--sample N] [--judge <conexión>] --json` |
| Ver resumen o casos | `gepa_dataset_show {datasetId, cases?}` | `gepa dataset show <id> [--cases] --json` |
| Aprobar | `gepa_dataset_approve {draftId, reviewedAllCases?}` | `gepa dataset approve <draftId> [--reviewed-all] --json` |

`gepa_dataset_show` acepta un borrador (`draft-…`) o una versión aprobada (`ds-…`).
`objective` es obligatorio al preparar. La previsualización toma por defecto 6
casos, y siempre casos de train y de val (en JEV, al menos uno por opción).

**Skill:** el artefacto es la carpeta de la Skill, y sus casos, su ejecutor y
su evaluador (comprobaciones o rúbrica con juez) tienen otro formato. Lee
[skill-cases.md](skill-cases.md) antes del paso 3 cuando el original sea una
Skill; la solicitud siguiente es la de una política JEV.

**Tarea nueva:** si ejecutar o puntuar la tarea exige algo que los adaptadores
incluidos no hacen (ejecutar código, un navegador, una API, otro sandbox u otro
evaluador), lee [new-adapter.md](new-adapter.md) antes del paso 3: con la
persona se prepara y comprueba un adaptador propio.

**Plantilla:** si en el paso 2 la persona elige una plantilla, lee
[templates.md](templates.md) antes del paso 3. Una plantilla es una carpeta del
proyecto que da el adaptador, el ejecutor, el evaluador y la presentación; los
casos son nuevos.

## Archivos intermedios

Los archivos que escribes solo para trabajar con el motor van en
`<home>/solicitudes/`: las solicitudes (`prepare.json`,
`guardar-plantilla.json`), las salidas que guardas para leerlas, los scripts de
apoyo y las copias de respaldo. `home` es la carpeta del motor, donde vive su
configuración: la dan `gepa setup check` y `gepa setup show`. Con la de por
defecto, la carpeta es `.gepa/solicitudes/` del proyecto. Créala si no existe y
da a cada archivo un nombre que diga qué es. Nunca los escribas en `/tmp`,
`$TMPDIR`, `$TEMP`, `%TEMP%`, `$env:TEMP` ni otra carpeta temporal del sistema:
quedarían fuera del proyecto y la persona no vería qué enviaste.

El motor resuelve una ruta relativa de una solicitud desde la carpeta de la
solicitud: desde `.gepa/solicitudes/`, `casos.xlsx` sería
`.gepa/solicitudes/casos.xlsx`. Usa rutas absolutas, con `/` también en Windows
(en los ejemplos, `<proyecto>` es la ruta absoluta del proyecto), o relativas a
esa carpeta (`../../casos.xlsx` con la de por defecto).

Los archivos para la persona no son intermedios: los casos cambiados van junto
a su archivo, según «Cambiar casos de un archivo de la persona».

## Solicitud de preparación

`prepare.json` (ver «Archivos intermedios»):

```json
{"artifact": {"type": "jev-policy", "path": "<proyecto>/policy.json"},
 "objective": "Qué comportamiento debe mejorar",
 "inputs": [
   {"path": "<proyecto>/casos.xlsx", "sheet": "Casos"},
   {"path": "<proyecto>/extra.csv"},
   {"table": "entrada\tesperado\n…", "origin": "Hoja del equipo, pegada por la persona"},
   {"cases": [{"input": "…", "expected": "alarm_set"}], "origin": "Propuestos por el agente en la conversación y confirmados por la persona"}
 ],
 "split": {"seed": 42}}
```

- `path` admite `.xlsx` (hoja `Casos` por defecto), `.csv` (con `,` o `;`, en
  UTF-8), `.tsv` y `.json` (lista de casos u objeto con `cases`).
- Columnas de tabla: `entrada` (obligatoria), `esperado` (un id de opción),
  `conjunto` (`train`, `val`, `test`; también entrenamiento, validación, prueba),
  y opcionales `id`, `procedencia`, `grupo`. Un mismo `grupo` marca casos
  relacionados que deben ir juntos.
- `split` fija la semilla de la división 60/20/20 cuando ningún caso trae
  `conjunto`. Si unos lo traen, todos deben traerlo.
- El motor registra en cada caso su `source`: archivo, SHA-256, hoja y fila, y
  el `origin` que declaras. `origin` es obligatorio en celdas pegadas y casos en
  línea; en un archivo declara de dónde vienen unos casos cambiados
  («Cambiar casos de un archivo de la persona»).

## Procedimiento

1. **Comprobar el entorno** con `gepa_setup_check` o `gepa setup check --json`.
   Listo cuando `ready` es `true`; si no, sigue setup-gepa.
2. **Acordar objetivo y original.** Pregunta qué decisión o tarea falla hoy y qué
   debe proteger. Después busca plantillas para ese tipo de original
   (`gepa_template_list {artifact}` o `gepa template list --artifact <tipo> --json`)
   y, si hay alguna, ofrécela con su `name` y su `description`. Si ninguna
   sirve, ofrece la plantilla de ejemplo de ese tipo: «Plantillas de ejemplo» en
   [templates.md](templates.md). Listo cuando
   tienes el original (la política o la carpeta de la Skill), un `objective` en
   una o dos frases con las palabras de la persona, y la persona eligió una
   plantilla o preparar sin ella.
3. **Reunir los casos** desde sus archivos, celdas pegadas o la conversación.
   Atribuye cada entrada a su origen real: «propuestos por el agente en la
   conversación y confirmados por la persona» para los que redactas tú; el nombre
   de un benchmark, solo cuando importas sus archivos. Decide qué comprobaciones
   son obligatorias según «Comprobaciones obligatorias». Para cambiar casos de un
   archivo de la persona, sigue «Cambiar casos de un archivo de la persona».
   Listo cuando cada entrada tiene su archivo o su `origin` y cada comprobación
   tiene decidida su marca.
4. **Preparar** y leer `issues`. Cada `error` bloquea: corrígelo con la persona
   (en un caso de sus archivos, según «Cambiar casos de un archivo de la
   persona») y vuelve a preparar. Explica cada `warning` y deja que la persona
   decida. Listo cuando `ready` es `true` y hay un `draftId`.
5. **Presentar el resumen** para que la persona confirme la alineación: que el
   objetivo corresponde a estos casos y a la forma de ejecutarlos y
   puntuarlos. Presenta `objective`, recuentos por partición,
   `coverage.expected` (casos por opción y partición), `coverage.sources`,
   `evaluator.description` y `criteria` (cada opción con su descripción); en una
   Skill o con un adaptador propio, lo que indica «Qué revisar con la persona»
   en [skill-cases.md](skill-cases.md) o en [new-adapter.md](new-adapter.md), y
   las marcas de obligatorio, como indica «Comprobaciones obligatorias». Con
   plantilla, nómbrala e incluye en esa explicación lo que ella decide de cómo
   se ejecutan y puntúan estos casos; con un adaptador propio, también qué hace
   su programa. Listo cuando la persona confirma que el problema, los casos y
   la forma de ejecutarlos y puntuarlos están alineados; un cambio vuelve al
   paso 3 o 4, y rechazar la plantilla lleva a preparar sin ella.
6. **Previsualizar el original.** Muestra por caso `input`, `expected`,
   `output`, `score`, `submetrics` y `feedback`; en una Skill o con un
   adaptador propio, también lo que indica «Qué revisar con la persona» en
   [skill-cases.md](skill-cases.md) o en [new-adapter.md](new-adapter.md). Pregunta si cada score coincide con
   su criterio: un desacuerdo revela un evaluador mal orientado, una rúbrica o un
   `expected` erróneos, y se corrige volviendo al paso 4 (un `expected` de un
   archivo de la persona, según «Cambiar casos de un archivo de la persona»).
   Listo cuando `status` es `complete` y la persona confirma que los scores
   tienen sentido caso por caso.
7. **Recomendar con firmeza revisar el dataset completo** (`review.recommendation`).
   Si acepta, muéstrale todos los casos con `gepa dataset show <draftId> --cases`
   por bloques, hasta el último. Listo cuando la persona revisó el último caso o
   decidió, tras la recomendación, aprobar sin revisarlos todos.
8. **Aprobar** solo tras un sí explícito. `reviewedAllCases: true` (o
   `--reviewed-all`) únicamente si la persona revisó todos los casos. Listo
   cuando tienes el `datasetId` (`ds-…`): entrégalo a optimize-with-gepa junto
   con el mismo original. Si la persona quiere repetir esta clase de prueba más
   adelante, ofrece guardarla como plantilla: «Guardar una plantilla» en
   [templates.md](templates.md).

## Comprobaciones obligatorias

Una comprobación con `"required": true` deja el caso en 0 si falla, sea cual
sea el resto. Sin la marca, un resultado equivocado en lo esencial casi
aprueba: con `file-checks`, el motor añade a cada caso `finished` (la sesión
terminó), así que un caso cuya única comprobación falla puntúa 0,5.

Al reunir los casos, con o sin plantilla, decide qué comprobaciones marcar
según lo que significa cada una, aunque la persona no lo pida:

- Marca la que mide lo esencial de la tarea, lo que la persona no aceptaría
  ver compensado por el resto: el archivo o el dato que pidió, un formato que
  otro programa lee.
- Deja sin marcar la que mide un aspecto que el resto puede compensar.
- Conserva las marcas que ya traen los casos. Si una te parece de más, díselo
  a la persona.
- Con una plantilla, parte de lo que su guía de casos (`cases.guide`) explica
  de las obligatorias, y compruébalo con el significado de las comprobaciones
  de los casos nuevos.

Admiten la marca las comprobaciones de una Skill ([skill-cases.md](skill-cases.md))
y los casos de un adaptador propio que la declara, como las pruebas de
`python-tests` (dónde la declara: «Resultados» en [new-adapter.md](new-adapter.md)).
Los casos de una política JEV con el adaptador incluido solo traen la opción
esperada: no hay nada que marcar.

Escribe cada marca donde está su caso: en la solicitud, si es un caso en línea
que redactaste; en un archivo nuevo, si viene de un archivo de la persona,
según «Cambiar casos de un archivo de la persona». Si los casos ya traen las
marcas que decidiste, no hay nada que cambiar.

En el resumen del paso 5, antes de previsualizar, dile a la persona qué
comprobaciones marcaste y por qué, y cuáles dejaste sin marcar. Ella puede
cambiarlo antes de aprobar.

## Cambiar casos de un archivo de la persona

Los archivos de casos que aporta la persona quedan como están. Cuando hay que
cambiar casos que vienen de uno de ellos (marcar una comprobación como
obligatoria, añadir o quitar comprobaciones, quitar un caso o corregir un dato,
también para resolver un `invalid-input` o un `error` de `issues`):

1. **Escribe un archivo nuevo** en la carpeta del de la persona, con todos sus
   casos y los cambios: `<nombre>-gepa-<N>.<extensión>`, con el primer `N`
   desde 1 cuyo archivo no exista (`casos.json` → `casos-gepa-1.json`). Nunca
   sobrescribas un archivo existente, tampoco uno que creaste antes: otra ronda
   de cambios parte del último archivo nuevo y toma el `N` siguiente. Conserva
   el formato; un libro de Excel (`.xlsx`, o un `.xls` que el motor no lee)
   pasa a CSV en UTF-8 con las mismas columnas.
2. **Prepara con él** en lugar del archivo de la persona, con un `origin` de
   hasta 500 caracteres que nombre ese archivo y todo lo que cambió respecto de
   él, y sin `sheet` si era un libro de Excel:
   `{"path": "<proyecto>/casos-gepa-1.json", "origin": "casos.json de la persona, con required: true en file-exists:output.csv de cada caso"}`.
3. **Díselo a la persona**: qué archivo creaste y qué cambió respecto del suyo.
   El resumen de la preparación no lo muestra: `coverage.sources` solo nombra el
   archivo nuevo.

Puedes ofrecer editar el archivo de la persona en su lugar. Edítalo solo cuando
ella lo acepte de forma expresa en esta conversación: un sí a tu oferta de
editar ese archivo, o que te pida editarlo. Una aprobación general
(«adelante», «déjalo listo», aprobar el dataset) no basta. Después dile qué
cambió y prepara con su archivo.

Un caso nuevo puede ir en el archivo nuevo o en línea (`cases`) con su
`origin`, como en el paso 3. Las celdas pegadas y los casos en línea están en
la solicitud: cámbialos allí y di en su `origin` qué cambiaste.

## Códigos

| code | Qué significa | Qué hacer |
| --- | --- | --- |
| `invalid-input` | Un archivo, tabla o entrada no se puede leer (formato, encabezado, codificación, fórmula, hoja) | Según el mensaje: un problema de la solicitud (un campo, `origin`, `sheet` o la forma de una entrada) se corrige en ella; uno de un archivo de la persona, según «Cambiar casos de un archivo de la persona». Preparar de nuevo |
| `invalid-policy` | La política no cumple el formato JEV `choice` | Corregirla con la persona |
| `invalid-skill` / `artifact-unreadable` | La carpeta de la Skill no existe, no tiene `SKILL.md` o trae un archivo binario o enlazado | Corregir la carpeta con la persona según el mensaje |
| `invalid-executor` | `executor` trae otro campo o un valor fuera de rango, o se indicó para una política | Corregirlo según [skill-cases.md](skill-cases.md) |
| `invalid-evaluator` | `evaluator` trae otro nombre, un campo, un peso o una rúbrica inválidos, o se indicó para una política | Corregirlo según [skill-cases.md](skill-cases.md) |
| `judge-invalid-output` / `judge-output-truncated` | El juez no dio un veredicto válido tras 3 reintentos (no cumplía el contrato o se cortaba); la previsualización falló sin puntuar | Probar otro modelo con `--judge`, o subir `evaluator.maxTokens` y preparar de nuevo |
| `invalid-models` | `--judge` en un borrador sin juez | Omitirlo, o preparar con `rubric-judge` |
| `invalid-adapter`, `adapter-*` o `infrastructure-error` con un adaptador propio | Su declaración, su código, sus requisitos, su entorno o su copia congelada | Seguir «Códigos» en [new-adapter.md](new-adapter.md) |
| `template-*`, `invalid-template`, `secret-in-template` o `program-changed` | La carpeta de la plantilla, su contenido, su programa o su combinación con la solicitud | Seguir «Códigos» en [templates.md](templates.md) |
| `workspace-unavailable` | No se pueden crear espacios aislados en la carpeta de datos | Seguir el siguiente paso del mensaje (permisos, espacio o `gepa setup data-dir`) |
| `draft-not-found` | El borrador no existe | Preparar de nuevo |
| `preview-missing` / `preview-failed` | Aprobar exige una previsualización completa del borrador | Previsualizar; si falla, resolver `error.code` con setup-gepa |
| `approval-outdated` | El adaptador o el evaluador instalados cambiaron desde la preparación | Preparar, previsualizar y aprobar de nuevo |
| `dataset-not-approved` | Un trabajo recibió un borrador o un id sin aprobar | Completar este procedimiento y usar el `ds-…` |
| `artifact-not-approved` | El trabajo usa otro original que el previsualizado (otra política, otro `SKILL.md` o recurso, u otro tipo) | Preparar y aprobar con ese original |
| `objective-not-approved` | El trabajo indica otro `objective` que el aprobado | Omitirlo, o preparar y aprobar con el nuevo objetivo |
| `credential-missing` / `role-missing` / `connection-missing` | La previsualización no tiene decisor, ejecutor o juez utilizable | Resolver con setup-gepa |

## Reglas

- Cambiar un caso, el original (la política, `SKILL.md` o un recurso), el
  ejecutor, el evaluador (rúbrica, peso o tokens del juez), el adaptador propio,
  la plantilla (otro contenido de su carpeta) o el objetivo produce otro
  borrador: vuelve a previsualizar y aprobar. Un trabajo anterior conserva su versión aprobada.
- La previsualización usa solo casos de `train` y `val`; `test` queda reservado
  para la evaluación final del trabajo.
- Las credenciales viven solo en las conexiones de setup-gepa.
