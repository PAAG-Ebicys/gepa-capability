# Plantillas de experimento

Una **plantilla** es una carpeta del proyecto que guarda una clase de tarea ya
comprobada para repetirla: el tipo de artefacto, el adaptador (el incluido, o
uno propio con sus archivos), los ajustes del ejecutor y del evaluador, la guía
para reunir casos y las reglas para presentar resultados. La carpeta es la
fuente de verdad: la persona o tú la leen, la editan y la copian a otro
proyecto. Nunca guarda casos, previsualizaciones, resultados, modelos ni
credenciales. Cada aplicación es un experimento nuevo: casos nuevos, un
borrador nuevo, el original ejecutado otra vez en la previsualización y una
aprobación propia.

Una plantilla entra en el proyecto o sale de él de tres formas: se copia una
plantilla de ejemplo de GEPA («Plantillas de ejemplo»), se prepara una copia
sin lo sensible para un compañero («Copia para compartir») o se trae de otro
proyecto, y entonces su programa espera la confianza de la persona
(«Programas traídos de fuera»).

## La carpeta

- `template.json`: la declaración. Solo lleva lo que una persona escribe o
  ajusta:
  - `format` (`"gepa-template-v2"`), `name`, `description` y `version`: un
    entero desde 1 para nombrarla en una conversación («la versión 3»).
  - `artifactType`: `jev-policy` o `skill`.
  - `adapter`: el incluido, por nombre y versión (`{"name": "skill-session",
    "version": "1"}` para una Skill, `{"name": "jev-choice", "version": "1"}`
    para una política), o `{"folder": "adapter"}` para uno propio.
  - `executor` y `evaluator`, solo con el adaptador incluido: el mismo formato
    que al preparar una Skill ([skill-cases.md](skill-cases.md)); `null` en una
    política.
  - `cases.guide`: cómo reunir casos de esta clase de tarea.
  - `presentation`: `metricLabel`, `submetrics` y `notes`.
  - `origin`, solo como información: el dataset, el borrador y la carpeta del
    proyecto de donde salió.
- `adapter/`: solo con un adaptador propio, sus archivos (`adapter.json`,
  `adapter.py` y sus recursos) tal como se aprobaron.

Lo que decide el adaptador no se escribe: el motor deduce el evaluador con su
regla de agregación (`evaluation`) y los campos de los casos (`cases.fields`)
cada vez que lee la carpeta.

Las plantillas del proyecto van en su carpeta de plantillas, por defecto
`gepa/plantillas/`. La da `gepa setup show` en `folders.templates`; la designa
setup-gepa.

## Operaciones

| Acción | Tool MCP | CLI |
| --- | --- | --- |
| Buscar | `gepa_template_list {artifact?}` | `gepa template list [--artifact jev-policy\|skill] --json` |
| Comprobar | `gepa_template_check {path}` | `gepa template check <carpeta> --json` |
| Guardar | `gepa_template_save {requestPath \| request}` | `gepa template save <guardar-plantilla.json> --json` |
| Copiar un ejemplo | `gepa_template_init {example, path?}` | `gepa template init [<carpeta>] --example <ejemplo> --json` |
| Confiar en un programa | — (solo la persona) | `gepa template trust <carpeta> --sha256 <sha>` |

`gepa template trust` solo existe en la CLI, y lo ejecuta la persona en su
terminal: ver «Programas traídos de fuera».

Dónde van `guardar-plantilla.json` y la solicitud de preparación, y cómo se
escriben sus rutas: «Archivos intermedios» en [SKILL.md](SKILL.md).

Con `valid: true`, `check` devuelve lo que el motor deduce, el `sha256` de la
carpeta y, en `apply.request`, la solicitud que la aplica. Con `valid: false`,
devuelve `problems`: cada uno con su `field`, su `code` y su `message`. Una
copia para compartir llega con `shareCopy: true` y sin `apply`: nunca se aplica.

Aplicar una plantilla es preparar con `"template": {"path": "<carpeta>"}` en la
solicitud, con el original, el objetivo y los casos nuevos, sin `executor`,
`evaluator` ni `adapter`:

```json
{"template": {"path": "<proyecto>/gepa/plantillas/armonizar-csv"}, "artifact": {"type": "skill", "path": "<proyecto>/skills/mi-skill"},
 "objective": "Qué comportamiento debe mejorar", "inputs": [{"path": "<proyecto>/casos-nuevos.json"}]}
```

## Aplicar una plantilla

La plantilla se elige en el paso 2 de la skill, con la lista filtrada por el
tipo del original. La persona la confirma en el resumen del paso 5 de
[SKILL.md](SKILL.md), junto con la alineación. Sigue estos pasos hasta ese
resumen.

1. **Comprobar** la carpeta con `check` y lee lo que devuelve: `cases.guide`,
   con la que reúnes los casos y decides sus marcas; `apply.request`, la
   solicitud que completas en «Preparar»; y `evaluation` (con
   `aggregation.rule` y, con juez, la rúbrica), el ejecutor y el adaptador y,
   con un adaptador propio, su `adapter.json`, para explicar en ese resumen lo
   que la plantilla decide. Con `valid: false`, dile a la persona qué problemas
   trae y corrígelos según «Editar» con su visto bueno, o prepara sin plantilla
   si lo prefiere. Listo cuando `check` da `valid: true`, o la persona decidió
   preparar sin plantilla (sigue entonces el paso 3 de la skill).
2. **Reunir los casos** según `cases.fields` y `cases.guide`, como en el paso 3
   de la skill, con sus marcas de obligatorio («Comprobaciones obligatorias» en
   [SKILL.md](SKILL.md)); la plantilla no trae casos. Listo cuando se cumple el
   «Listo» del paso 3 de la skill.
3. **Preparar** con `"template": {"path": "<carpeta>"}` y sigue los pasos 4 a 8
   de la skill, cada uno hasta su «Listo»: previsualiza solo cuando la persona
   confirmó el resumen del paso 5, con las marcas de obligatorio. Si en el
   resumen la persona pide cambiar algo que fija la plantilla, edítala según
   «Editar» y prepara de nuevo; si decide no usarla, prepara sin plantilla. Si la
   preparación da `template-untrusted`, sigue «Programas traídos de fuera» y
   vuelve a preparar cuando la persona haya confiado. El motor
   comprueba la plantilla sobre el original nuevo y guarda una copia congelada:
   el borrador, su aprobación y sus trabajos usan esa copia aunque la carpeta
   cambie después. El borrador lleva en `template` la ruta, el `sha256`, el
   nombre, la versión, la presentación y un `applicationId` propio. Cada
   preparación con `template` es una aplicación nueva, también con los mismos
   casos, y necesita su previsualización y su aprobación. Listo cuando tienes
   el `ds-…`.

## Editar

Una plantilla se edita cambiando sus archivos, sobre todo `template.json`: no
hay un comando para editarla. Después de cada cambio:

1. Si el contenido cambió, sube `version` en 1, para que la persona pueda
   nombrar la versión nueva. El motor distingue cada contenido por el SHA-256
   de la carpeta, aunque dos contenidos declaren la misma `version`.
2. Comprueba con `gepa template check <carpeta>`. Los problemas llegan campo
   por campo: corrígelos y vuelve a comprobar. `check` nunca ejecuta el
   programa de un adaptador propio: lee su `adapter.json`. La comprobación
   completa, sobre el original, ocurre al aplicar.

- `executor` y `evaluator`: `null` vuelve al valor por defecto.
- Un adaptador propio se cambia en sus archivos de `adapter/`
  ([new-adapter.md](new-adapter.md)).
- `evaluation` y `cases.fields` no se escriben: los deduce el motor.
- Un borrador ya preparado sigue con su copia congelada: para usar los
  cambios, prepara de nuevo.

## Guardar una plantilla

Ofrécelo cuando un dataset quedó aprobado y la persona quiere repetir esa clase
de prueba con otros casos u otro original.

```json
{"from": "ds-0123456789abcdef", "name": "Armonizar CSV de laboratorio",
 "description": "Una Skill transforma un CSV y se comprueba el archivo producido",
 "cases": {"guide": "Cada caso trae input.csv y comprobaciones file-exists y file-equals sobre output.csv. Las dos son obligatorias: otro programa lee output.csv tal cual, así que un archivo que falta o que difiere no sirve"},
 "presentation": {"metricLabel": "…", "submetrics": {"…": "…"}, "notes": ["…"]}}
```

- `from` es un dataset aprobado (`ds-…`): la plantilla sale de una preparación
  comprobada. Con un adaptador propio, la carpeta lleva en `adapter/` la copia
  que se aprobó, aunque su carpeta de origen haya cambiado.
- El motor escribe una carpeta nueva con `version: 1` en la carpeta de
  plantillas (`folders.templates` de `gepa setup show`), con un nombre sacado
  de `name`. Con `path`, la escribe en esa carpeta, relativa a la de la
  solicitud. Nunca sobrescribe: si la carpeta ya existe, da `template-exists`.
- Escribe `cases.guide`, las etiquetas y las notas con palabras de la clase de
  tarea. Describe la forma de los casos; cada caso concreto queda en su dataset.
- En `cases.guide`, explica qué comprobaciones fueron obligatorias y por qué, o
  que ninguna lo fue. La marca va en cada caso, así que la guía es lo único
  que la plantilla conserva de ella: con esa explicación, quien la aplique a
  casos nuevos repite la misma exigencia.
- Listo cuando tienes la carpeta (`path` en la respuesta) y la persona sabe que
  se aplica con `template` en una preparación nueva, también en otro proyecto
  si copia la carpeta. Dile también que puedes preparar una copia para
  compartirla con un compañero o guardarla en git, sin lo sensible: «Copia
  para compartir».

## Plantillas de ejemplo

GEPA trae dos plantillas de ejemplo, para no empezar desde cero:

- `skill-file-checks`: una Skill lee los archivos de cada caso, escribe su
  resultado y se puntúa con comprobaciones de archivos (`file-checks`).
- `jev-policy`: una política JEV elige una opción por solicitud y se compara
  con la opción esperada.

Las dos usan un adaptador incluido y ningún programa propio, así que se aplican
sin confiar en nada. Ofrécelas en el paso 2 de la skill cuando ninguna
plantilla del proyecto sirva para el original.

1. **Copiar** el ejemplo con `gepa template init --example <ejemplo> --json`
   (o `gepa_template_init {example}`). Sin carpeta, la copia va a la carpeta de
   plantillas, con el nombre del ejemplo; con `<carpeta>` (o `path`), a esa
   carpeta nueva. Nunca sobrescribe: si la carpeta ya existe, da
   `template-exists`. Listo cuando tienes la carpeta en `path`.
2. **Adaptar** lo que la tarea necesita, según «Editar»: `name`,
   `description`, `cases.guide`, `presentation` y, en una Skill, `executor` y
   `evaluator`. La persona lo confirma en el resumen del paso 5 de la skill:
   dile allí qué cambiaste y cómo quedó, con el nombre nuevo de la plantilla.
   Listo cuando `gepa template check` da `valid: true`.
3. **Aplicar** según «Aplicar una plantilla». Listo cuando se cumple el
   «Listo» de su paso «Preparar».

## Copia para compartir

Una copia para compartir es una copia de una plantilla de la persona en la que
todo lo sensible se reemplaza por huecos, para darla a un compañero o guardarla
en git: el `.gitignore` del setup deja fuera las plantillas, pero no sus copias
`.compartir/`. La haces tú, no el motor, porque decidir qué es sensible exige
juzgar su significado. Prepárala solo cuando la persona la pida.

1. **Copiar** la carpeta de la plantilla a `<nombre>.compartir/`, junto a la
   original: `<proyecto>/gepa/plantillas/armonizar-csv/` →
   `<proyecto>/gepa/plantillas/armonizar-csv.compartir/`. Trabaja en tu
   borrador de la copia; no escribas la carpeta hasta el paso 4. Listo cuando
   tienes el borrador con todos los archivos de la plantilla.
2. **Reemplazar** por huecos visibles, como `«HUECO: el proceso de cierre de
   tu equipo»`, todo lo sensible según su significado: credenciales, procesos
   privados de una empresa y datos ajenos a la tarea (nombres de clientes o
   personas, rutas y servidores internos). Revisa cada texto de
   `template.json`, también `origin`, y cada archivo del programa del
   adaptador en `adapter/`: si el programa contiene o revela algo de eso,
   reemplaza sus archivos por un hueco que diga qué hacía. Revisa también el
   nombre de la carpeta: si revela algo de eso, como el nombre de un cliente,
   propón otro que termine en `.compartir`. Lo que describe la clase de tarea
   sin nada sensible se queda. Listo cuando cada parte sensible del borrador es
   un hueco y el nombre de la carpeta es neutro.
3. **Decir** a la persona qué ocultaste y por qué, hueco por hueco (también el
   nombre de la carpeta, si lo cambiaste), y qué dejaste porque no es sensible.
   Listo cuando la persona responde.
4. **Escribir** la copia solo con su visto bueno expreso: un sí a la copia que
   le mostraste. Va a git, así que una aprobación general («adelante»)
   anterior a ver los huecos no basta. Listo cuando la carpeta existe y la
   persona sabe dónde está.

El motor la reconoce por el sufijo `.compartir`: la omite del listado, `check`
la muestra con `shareCopy: true` y la preparación la rechaza con
`template-is-share-copy`. Quien la recibe la copia a una carpeta con otro
nombre, rellena sus huecos y la comprueba con `gepa template check`.

## Programas traídos de fuera

Una plantilla con un adaptador propio trae un programa en `adapter/`. Si este
proyecto nunca lo ejecutó (la plantilla vino de otro proyecto) y la persona no
confió en él, la preparación da `template-untrusted` antes de cargarlo. Las
plantillas guardadas en este proyecto y las de ejemplo no preguntan. El error
trae en `trust` el comando de confiar (`trust.command` y, para PowerShell,
`trust.powershell`), el `sha256` del programa y sus archivos (`trust.files`).

1. **Explicar** a la persona qué hace el programa: lee
   `adapter/adapter.json` (entrada, recursos, herramientas, entorno, red,
   ejecución de código, evaluación y requisitos) y `adapter/adapter.py`, sin
   ejecutarlo, y dile en palabras qué ejecuta, qué archivos lee o escribe y a
   qué red o servicio llama. Listo cuando lo sabe.
2. **Mostrar** sus archivos: cada uno de `trust.files`, o los que pida la
   persona. Listo cuando la persona los vio o dijo que no los necesita.
3. **Dar** el comando tal cual, para que lo ejecute ella misma: en Claude Code,
   `trust.command` con el prefijo `!` (`! <comando>`); en otro anfitrión, para
   pegarlo en su terminal (`trust.powershell` en PowerShell). Listo cuando la
   persona tiene el comando.
4. **Esperar** a que la persona diga que lo ejecutó, y prepara de nuevo. Si
   decide no confiar, prepara sin esa plantilla. Listo cuando la preparación ya
   no da `template-untrusted`.

Nunca ejecutes el comando de confiar por tu cuenta, tampoco con un sí de la
persona: el motor no sabe quién lo ejecuta, y la regla es que lo haga ella. No
rodees la regla: no ejecutes `gepa adapter check` sobre `adapter/` (ejecuta
el `check()` del programa) ni prepares con `"adapter": {"path": "<carpeta>/adapter"}`
(cargaría el programa como adaptador suelto) antes de que la persona confíe.
Confiar solo existe en la CLI: no hay tool MCP que lo haga. El comando lleva
el `sha256` del programa que explicaste: si el programa cambió, da
`program-changed` y no registra nada; vuelve a preparar para ver el nuevo.

## Códigos

| code | Qué significa | Qué hacer |
| --- | --- | --- |
| `template-not-found` | La ruta no es una carpeta con `template.json` | Buscarla con la lista o corregir la ruta |
| `template-exists` | Al guardar, la carpeta ya existe | Elegir otro `name` o indicar otra `path`; nunca borrar la carpeta que existe |
| `template-artifact-mismatch` | La plantilla es para otro tipo de artefacto | Elegir otra plantilla o preparar sin plantilla |
| `template-conflict` | La solicitud trae `executor`, `evaluator` o `adapter` junto con `template` | Quitarlos, o editar los archivos de la plantilla para cambiarlos |
| `template-is-share-copy` | La carpeta es una copia para compartir (`<nombre>.compartir/`), con huecos por rellenar | Copiarla a una carpeta con otro nombre, rellenar sus huecos y comprobarla: «Copia para compartir» |
| `template-untrusted` | La plantilla trae un programa propio que este proyecto nunca ejecutó y en el que la persona no confió; no se cargó | Explicar el programa y dar a la persona `trust.command` para que lo ejecute ella: «Programas traídos de fuera». Nunca ejecutarlo tú |
| `program-changed` | Al confiar, el programa de `adapter/` ya no es el del comando | Preparar de nuevo para ver el programa nuevo y explicarlo antes de dar el comando nuevo |
| `invalid-template` | Un campo de `template.json` o de la solicitud de guardar no es válido, o la solicitud nombra la plantilla por un id antiguo en lugar de su carpeta | Corregirlo según el mensaje y volver a comprobar |
| `secret-in-template` | Un campo parece una credencial, por su nombre o por su texto (una clave de proveedor o la de una conexión configurada). El mensaje nombra el campo, nunca el valor | Quitarla: las credenciales viven en las conexiones de setup-gepa |
| `dataset-not-approved` | `from` no es un dataset aprobado | Aprobarlo antes de guardar la plantilla |
| `invalid-executor` / `invalid-evaluator` | Ajustes inválidos, o indicados con un adaptador propio | Corregirlos según [skill-cases.md](skill-cases.md) |
| `invalid-adapter` / `adapter-unreadable` | La carpeta `adapter/` o su `adapter.json` no son válidos | Corregirlos según «Códigos» en [new-adapter.md](new-adapter.md) |
| `template-write-failed` | No se pudo escribir la carpeta al guardar | Revisar permisos y espacio libre, o elegir otra `path` |
