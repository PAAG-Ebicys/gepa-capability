---
name: optimize-with-gepa
description: Optimiza con GEPA una política de decisión JEV o una Skill usando un dataset aprobado, y compara el original con los candidatos. Úsala cuando la persona pida "optimiza esta política" u "optimiza esta skill", "mejora las instrucciones, los criterios o el SKILL.md con estos ejemplos", o quiera iniciar, seguir, cancelar, reintentar o exportar un trabajo GEPA.
license: MIT
compatibility: Requiere el motor instalado y un dataset aprobado. MCP es opcional; la CLI `gepa` da el mismo resultado.
metadata:
  version: "0.1.0"
  engine: "gepa==0.1.4"
---

# optimize-with-gepa

Lleva una política JEV de tipo `choice` o una Skill desde un dataset aprobado
hasta un candidato exportado o hasta la recomendación de conservar el original.
El motor busca y puntúa; tú preparas el trabajo, lo sigues e interpretas la evidencia.

## Operaciones

Cada tool del servidor MCP `gepa` y su comando CLI devuelven el mismo JSON.
Usa la tool si el anfitrión la expone; si no, la CLI con el comando de
`engine.md` (en la carpeta de esta skill; `gepa` lo abrevia en la tabla).

| Acción | Tool MCP | CLI |
| --- | --- | --- |
| Iniciar | `gepa_job_start {specPath}` | `gepa job start <trabajo.json> --json` |
| Consultar | `gepa_job_show {jobId, cases?}` | `gepa job show <jobId> [--cases] --json` |
| Listar | `gepa_job_list` | `gepa job list --json` |
| Cancelar | `gepa_job_cancel {jobId}` | `gepa job cancel <jobId> --json` |
| Reintentar | `gepa_job_retry {jobId, kind?}` | `gepa job retry <jobId> [--kind <recuperación>] [--detach] --json` |
| Revisar | `gepa_job_review {jobId}` | `gepa job review <jobId> --json` |
| Exportar | `gepa_job_export {jobId, outDir, candidateId?}` | `gepa job export <jobId> --out <carpeta-nueva> [--candidate <id>] --json` |

`gepa_job_start` y `gepa_job_retry` ejecutan el trabajo en un proceso aparte y
vuelven de inmediato con el trabajo recién lanzado (normalmente `queued`; el
reintento puede llegar ya `running`): el trabajo sigue aunque termine tu sesión,
y cualquier sesión posterior puede consultarlo o cancelarlo. `gepa job start` y
`gepa job retry` esperan hasta el final (el primero informa el `jobId` al
empezar); con `--detach` vuelven de inmediato como la tool.

## Archivos intermedios

Los archivos que escribes solo para trabajar con el motor van en
`<home>/solicitudes/`: las solicitudes (`trabajo.json`), las salidas que
guardas para leerlas, los scripts de apoyo y las copias de respaldo. `home` es
la carpeta del motor, donde vive su configuración: la dan `gepa setup check` y
`gepa setup show`. Con la de por defecto, la carpeta es `.gepa/solicitudes/`
del proyecto. Créala si no existe y da a cada archivo un nombre que diga qué
es. Nunca los escribas en `/tmp`, `$TMPDIR`, `$TEMP`, `%TEMP%`, `$env:TEMP` ni
otra carpeta temporal del sistema: quedarían fuera del proyecto y la persona no
vería qué enviaste.

El motor resuelve una ruta relativa de una solicitud desde la carpeta de la
solicitud: desde `.gepa/solicitudes/`, `policy.json` sería
`.gepa/solicitudes/policy.json`. Usa rutas absolutas, con `/` también en
Windows (en los ejemplos, `<proyecto>` es la ruta absoluta del proyecto), o
relativas a esa carpeta (`../../policy.json` con la de por defecto).

## Archivos

`policy.json` — la política original. Los id de opción y su orden son fijos;
GEPA solo reescribe `instructions` y la descripción de cada opción:

```json
{"question": "intent", "type": "choice", "instructions": "Clasifica la intención de la solicitud.",
 "criteria": {"alarm_set": "Crear o programar una alarma", "alarm_query": "Consultar las alarmas"}}
```

`trabajo.json` (ver «Archivos intermedios»):

```json
{"name": "Intenciones", "artifact": {"type": "jev-policy", "path": "<proyecto>/policy.json"}, "dataset": "ds-…",
 "objective": "Qué comportamiento debe mejorar", "limits": {"maxMetricCalls": 120, "maxProposals": 5, "timeLimitMinutes": 60}, "seed": 42}
```

`dataset` es el `ds-…` que devolvió la aprobación, y `artifact` el mismo original
que se previsualizó al aprobarlo. `objective` es opcional: el trabajo usa el aprobado.
`models` es opcional (`{"decider": "<conexión>", "reflection": "<conexión>"}`);
por defecto usa los roles `executor` y `reflection` de setup-gepa.
Los límites son de este trabajo, no del dataset. `maxProposals` cuenta
**iteraciones de búsqueda de GEPA**, no propuestas individuales ni candidatos que necesariamente llegan a
validación completa: una propuesta puede perder o empatar en la muestra de
entrenamiento y quedar descartada. `maxMetricCalls` limita evaluaciones de
búsqueda y `timeLimitMinutes` limita cada intento completo, incluida la prueba
final cuando corresponda. El motor no reserva automáticamente tiempo para esa
prueba. Opcionalmente,
`limits.validationScoreTarget` admite un número de 0 a 1 para detener la
búsqueda cuando el mejor resultado en **validación completa** alcanza esa meta;
si el original ya la cumple, puede parar antes de proponer nada. En una tarea
de exactitud, 0,8 es 80 % de aciertos; en una rúbrica, es puntuación media 0,8.

**Skill:** `artifact` es la carpeta de la Skill, y GEPA reescribe solo su `SKILL.md`
(el `name` del frontmatter y los demás archivos quedan fijos):

```json
{"name": "Armonización", "artifact": {"type": "skill", "path": "<proyecto>/skills/lab-unit-harmonization"}, "dataset": "ds-…",
 "limits": {"maxMetricCalls": 120, "maxProposals": 5, "timeLimitMinutes": 60}}
```

En `models`, la Skill usa `executor` en lugar de `decider`, y `judge` cuando la
aprobación selló el evaluador `rubric-judge` (por defecto, el rol `judge`). Cada
caso corre en un espacio aislado con el ejecutor que selló la aprobación (turnos
y tokens por turno); `limits` solo fija presupuesto, tiempo y la reflexión. La
rúbrica, el peso de las comprobaciones y los tokens del juez son de la
aprobación, no del trabajo.

**Adaptador propio:** el trabajo ejecuta la copia del adaptador que selló la
aprobación, y `models` admite los roles que declara (`executor` y, si su
evaluador consulta un juez, `judge`).

## Procedimiento

1. **Comprobar el motor** con `gepa_setup_check` o `gepa setup check --json`.
   Resuelve con setup-gepa los hallazgos que afecten a este trabajo. Un rol
   global pendiente no bloquea si `models` indica una conexión válida para ese
   rol. Listo cuando el motor y las conexiones necesarias responden.
2. **Obtener un dataset aprobado** con prepare-gepa-experiment: casos
   validados, el original previsualizado y la aprobación de la persona. `train`
   alimenta la reflexión, `val` elige el candidato y `test` queda reservado para
   comprobar la selección si existe una mejora validada. Listo cuando tienes
   un `datasetId` `ds-…`.
3. **Cerrar modelos y límites con la persona antes de iniciar.** Elige
   conexiones para el ejecutor/decisor, la reflexión y el juez solo si la
   evaluación lo requiere; usa `models` para elecciones de este trabajo o los
   roles globales ya asignados. Recupera las preferencias de tiempo y meta de
   validación anotadas al preparar el dataset, si las hay. Concreta
   `maxMetricCalls`, `maxProposals`, `timeLimitMinutes` y, opcionalmente,
   `validationScoreTarget`; muestra qué significa cada límite y estima si el
   presupuesto alcanza para validar el original, probar varias iteraciones y
   dejar tiempo para la comprobación final. Convierte un porcentaje en número de
   aciertos solo cuando la métrica principal es exactitud. Advierte si la meta
   ya la cumple el original: eso puede parar la búsqueda inmediatamente.
   Listo cuando la persona conoce los modelos y límites efectivos del trabajo.
4. **Iniciar el trabajo.** Los errores de preparación llegan antes de gastar
   presupuesto como `{"error", "code"}` (en la tool, o en stdout con `--json`).
   Decide por el `code`, explícalo y corrige con la persona:
   - con prepare-gepa-experiment: `dataset-not-approved`, `artifact-not-approved`,
     `objective-not-approved`, `approval-outdated`, `adapter-changed`,
     `adapter-missing`;
   - con `gepa adapter check`: `adapter-requirement-missing`;
   - con setup-gepa: `role-missing`, `connection-missing`, `credential-missing`,
     `workspace-unavailable`;
   - en `trabajo.json` o en el original: `budget-too-small`, `invalid-policy`,
     `invalid-skill`. Listo cuando la
   respuesta trae un `jobId` y tienes su `status`.
5. **Seguir** con la consulta hasta que `status` sea `completed`, `stopped`,
   `failed`, `cancelled` o `interrupted`. `phase` dice dónde está o dónde
   terminó (`preparation`, `search`, `final`); `events` cuenta lo ocurrido.
   `consumption.search` compara las evaluaciones con el presupuesto de búsqueda,
   y `consumption.final` da los casos resueltos (`resolved`) de los requeridos
   (`required`) en la prueba reservada cuando se ejecuta; las dos dicen
   llamadas, tokens, coste y segundos. Explica cada propuesta como generada,
   probada en muestra, descartada o llevada a validación completa según sus
   eventos. Una iteración consumida sin candidato validado no demuestra una
   mejora. Si la selección conserva el original, el trabajo omite `test` para
   reservarlo para una búsqueda futura. Informa del progreso cuando la persona
   lo pida, no en cada consulta.
   - **Cancelar** solo cuando la persona lo pida. Un trabajo en curso responde
     con `cancelRequested` y se detiene antes de su siguiente llamada: sigue
     consultando hasta `cancelled`.
   - **Terminado sin completar:** `stopReason` o `error.code` dan el motivo, y
     la evidencia parcial se conserva; solo `completed` es exportable.
     `interrupted` significa que el proceso terminó sin cerrar el trabajo (se
     cerró, falló o se reinició el equipo).
   - **Reintentar:** con `recovery.retryable` en `true`, explica
     `recovery.message` a la persona y ofrece también crear un trabajo nuevo;
     reintenta solo si lo acepta. Si `recovery.options` trae dos recuperaciones
     (`recovery.kind` es `null`), pregúntale cuál quiere y explícale el
     `message` de cada una:
     - `continue-search`: seguir buscando desde el último estado guardado de
       GEPA, como después de una pausa;
     - `final-retry`: cerrar con los candidatos ya validados; solo se usa la
       prueba reservada si alguno supera al original.

     Reintenta con el `kind` que elija. Con `search-state-missing`,
     `search-state-damaged` o `search-state-mismatch` no se puede continuar:
     ofrece cerrar con lo validado o crear un trabajo nuevo. Con
     `search-not-recoverable`, crea un trabajo nuevo con la misma
     especificación, o con otros modelos o límites.
   - **Causas que no son del candidato:** en una Skill, `infrastructure-error`
     o `workspace-unavailable` (falló el entorno) y `judge-invalid-output` o
     `judge-output-truncated` (el juez no dio un veredicto válido tras 3
     reintentos); el caso afectado no recibió score. Un juez inadecuado se
     cambia con `models.judge` en un trabajo nuevo; sus tokens, preparando y
     aprobando otra vez. Con un adaptador propio, `adapter-invalid-result` y
     `adapter-error` (el adaptador rompió su contrato o falló) e
     `infrastructure-error` (falló el entorno de la tarea): se corrige el
     adaptador, se prepara y aprueba de nuevo y se crea otro trabajo.
6. **Revisar con review-gepa-results.** Esa skill lee el informe de
   `gepa_job_review` o `gepa job review <jobId> --json` y explica la
   comparación por caso, los límites de la evidencia y la recomendación (conservar el
   original salvo mejora demostrada). La vista de `gepa job show` solo dice qué
   candidato eligió la validación. El paso termina cuando la persona conoce la
   recomendación del informe y su motivo.
7. **Exportar** solo cuando la persona lo pida, a una carpeta nueva fuera de la
   carpeta del original. Listo cuando existen `policy.json` (o `skill/`, con el
   `SKILL.md` del candidato y los recursos originales), `manifest.json` y
   `evidence.json`, y el original sigue intacto. Instalar lo exportado es una
   decisión aparte de la persona.

## Reglas

- Las credenciales viven solo en las conexiones de setup-gepa.
- Cuando se ejecuta, `test` comprueba la selección ya hecha por validación; una
  nueva versión del original necesita un dataset aprobado nuevo y otro trabajo.
