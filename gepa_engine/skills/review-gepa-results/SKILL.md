---
name: review-gepa-results
description: Revisa la evidencia de un trabajo GEPA ya ejecutado, completo o parcial, sin llamar a modelos. Compara el original con hasta cinco candidatos caso por caso y recomienda conservar el original o exportar un candidato. Úsala cuando la persona pregunte qué resultado dio una optimización, por qué un candidato acierta o falla en un caso, cuál conviene exportar, o quiera reexaminar un trabajo antiguo.
license: MIT
compatibility: Requiere el motor instalado con setup-gepa. MCP es opcional; la CLI `gepa` da el mismo resultado.
metadata:
  version: "0.1.0"
  engine: "gepa==0.1.4"
---

# review-gepa-results

Explica un trabajo desde su **evidencia** guardada: dónde acertó o falló el
original y cada finalista, sobre qué denominador y con qué límites. El motor
calcula el informe a partir de los registros; tú lo interpretas con la persona.
Revisar nunca vuelve a ejecutar la inferencia: si falta una respuesta, eso se
informa como falta de evidencia.

## Operaciones

Cada tool del servidor MCP `gepa` y su comando CLI devuelven el mismo JSON.
Para la CLI, usa el comando de `engine.md` (en la carpeta de esta skill; `gepa`
lo abrevia en la tabla).

| Acción | Tool MCP | CLI |
| --- | --- | --- |
| Encontrar el trabajo | `gepa_job_list` | `gepa job list --json` |
| Informe | `gepa_job_review {jobId}` | `gepa job review <jobId> --json` |
| Otros candidatos (hasta 5) | `gepa_job_review {jobId, candidateIds}` | `gepa job review <jobId> --candidate <id> [--candidate <id>…] --json` |
| Un caso completo | `gepa_job_review {jobId, caseId}` | `gepa job review <jobId> --case <caseId> --json` |
| Una fila por caso | `gepa_job_review {jobId, cases: true}` | `gepa job review <jobId> --cases --json` |
| Exportar | `gepa_job_export {jobId, outDir, candidateId}` | `gepa job export <jobId> --out <carpeta-nueva> --candidate <id> --json` |

Sin `candidateIds`, el informe compara el original con los finalistas. El
original siempre está incluido; pedir más de cinco candidatos da
`too-many-candidates`.

## Archivos intermedios

Los archivos que escribes solo para trabajar con el motor van en
`<home>/solicitudes/`: las salidas que guardas para leerlas (por ejemplo, el
JSON del informe para comparar `candidates[]`), los scripts de apoyo y las
copias de respaldo. `home` es la carpeta del motor, donde vive su
configuración: la da `gepa setup show --json` (o `gepa_setup_show`), que no
llama a modelos. Con la de por defecto, la carpeta es `.gepa/solicitudes/` del
proyecto. Créala si no existe y da a cada archivo un nombre que diga qué es.
Nunca los escribas en `/tmp`, `$TMPDIR`, `$TEMP`, `%TEMP%`, `$env:TEMP` ni otra
carpeta temporal del sistema: quedarían fuera del proyecto y la persona no
vería qué hiciste.

## Procedimiento

1. **Identificar el trabajo.** Busca el `jobId` con la lista (por nombre, fecha o
   `datasetId`). El paso termina cuando tienes un `jobId` que la persona confirma.
2. **Leer el informe** y explicar a la persona, en este orden:
   - `completeness.status`. Si es `incomplete`, di qué falta con
     `completeness.message`. Un `score` en `null` significa que no hay porcentaje:
     informa `evaluated` de `total` casos. Si el trabajo se detuvo antes de
     completarse, `recovery` de `gepa_job_show` o `gepa job show <jobId> --json`
     dice cómo puede recuperarse con optimize-with-gepa (una búsqueda cortada
     puede continuar o cerrar con lo validado, a elección de la persona).
   - `target.scope`: el modelo, el juez si lo hay, el adaptador (con el
     `sha256` de su carpeta si es propio) y el evaluador para los que vale la
     evidencia.
   - `primaryMetric` frente a `submetrics`. La recomendación usa solo la métrica
     principal; `primaryMetric.caseAggregation.rule` dice cómo se forma el score
     de un caso. Una submétrica que mejora sola no es una mejora. Por partición,
     una comprobación se cuenta como `passed` de `evaluated`, y un score (`judge`,
     `rubric:<criterio>`) como `mean`; `requirementsMet` cuenta los casos que
     cumplieron todos sus requisitos obligatorios y `taskAccuracy` los que
     cumplieron todas sus comprobaciones (en `null`, el evaluador no la tiene).
   - `counts`. Distingue casos únicos (`cases`, los denominadores), evaluaciones,
     llamadas por modelo, iteraciones y `internalChecks`. Las comprobaciones
     internas ocurren dentro de un caso y no son casos nuevos.
   - `costUsd`: si es `null`, el coste es desconocido, no cero.
   - `testObservedBefore`: qué trabajos anteriores ya habían evaluado esos casos
     de prueba.

   El paso termina cuando cada uno de estos seis puntos quedó dicho con sus
   números.

   Si `presentation` no es `null`, el trabajo se preparó con una plantilla.
   `presentation.template` la nombra por su nombre, su versión y su `sha256`
   (el contenido exacto que se congeló), y por la ruta de su carpeta; un
   trabajo de antes de que las plantillas fueran carpetas la nombra por su
   `templateId`. Nombra la métrica principal con
   `metricLabel` y cada submétrica con su etiqueta de `submetrics`, junto al
   nombre guardado, y usa `notes` para explicar los resultados. La plantilla
   solo nombra y explica: cada número, la selección y la recomendación salen de
   la evidencia de este trabajo.
3. **Comparar** el original y los candidatos por partición.
   - `comparison.<validation|test>.sameDenominator` indica si todos cubren los
     mismos casos.
   - `candidates[].vsOriginal.test` da los `better` y `worse`: los id de los casos
     donde cada candidato mejora o empeora frente al original.
   - La validación sirvió para elegir; la prueba reservada es la comprobación
     independiente.
4. **Presentar la recomendación** con `recommendation.message` como mensaje
   principal. Su `reason` es uno de estos:

   | `reason` | Acción | Qué decir |
   | --- | --- | --- |
   | `improved` | `adopt-candidate` | El seleccionado supera al original en validación y en una prueba reservada completa que no se había observado antes. |
   | `incomplete` | `keep-original` | Falta evidencia: no hay porcentaje ni ganador. |
   | `original-selected` | `keep-original` | Ningún candidato superó al original en validación. |
   | `no-test-improvement` | `keep-original` | El seleccionado ganó en validación, pero no en la prueba reservada. |
   | `test-previously-observed` | `keep-original` | La mejora aparece en una prueba ya observada: no es evidencia independiente nueva. |

   Los demás finalistas se inspeccionan y se exportan solo si la persona lo pide.
   Un finalista no pasa a ser el recomendado por puntuar más en la prueba: elegir
   con la prueba reservada la convierte en entrenamiento.
   Cierra la recomendación con `limits`. El paso termina cuando la persona conoce
   la acción, el motivo y los límites.
5. **Decir qué cambió** cada candidato que presentes (el seleccionado y los que
   la persona pida ver o exportar), en la misma respuesta que la recomendación.
   Compara su artefacto en `candidates[]` del JSON del informe con el del
   original, el primero de la lista (`role: original`). Son los textos
   evaluados; la vista de texto no los muestra:
   - en una política JEV, `policy`: `instructions` y la descripción de cada
     opción en `criteria`, lo único que GEPA puede cambiar;
   - en una Skill, `skill`: su `SKILL.md`.

   Di en palabras qué se añadió, qué se quitó y qué se reescribió, con citas
   breves de lo que cambia. Si el cambio es mínimo (por ejemplo, una sola
   palabra), señálalo como mínimo y sugiere a la persona abrir con `caseId`
   (paso 6), antes de exportar, los casos donde el candidato mejora: los id de
   `better` en `vsOriginal.test` y en `vsOriginal.validation`. Una partición en
   `null` no tiene casos comunes con el original; si no queda ningún caso que
   abrir, dilo. Así la persona ve si ese cambio explica la mejora.

   `recommendation` es la del motor: preséntala tal cual, y el cambio como
   información. No hay umbral fijo: la valoración del cambio es de la persona.
   Cierra con sus opciones (exportar, abrir los casos que mejoran, comprobar el
   candidato con otro modelo, conservar el original) y pregúntale cuál
   prefiere. El paso termina cuando cada candidato presentado tiene su
   descripción de lo añadido, lo quitado y lo reescrito, con el aviso si su
   cambio es mínimo, y la persona tiene sus opciones.
6. **Abrir casos** cuando la persona quiera entender una diferencia. Usa `caseId`
   con los id de `better` o `worse`, o `cases: true` para ver la tabla completa.
   Si comparas candidatos que no son finalistas, repite sus `candidateIds`: sin
   ellos, el caso trae solo el original y los finalistas.
   Cada evaluación trae entrada, esperado, salida, decisión, score, submétricas,
   feedback, error y traza (latencia, tokens, coste) tal como se guardaron. En una
   Skill, en lugar de la decisión: `requirementsMet`, `checks` (cada comprobación
   con lo obtenido frente a lo esperado), `effects` (archivos creados o
   modificados, con su SHA-256), `session` (cómo terminó y sus acciones) y, con
   juez, `judge` (score, razón por criterio y resumen). Con un adaptador propio:
   `observation` (lo que devolvió su `execute`), `effects` y `steps` (llamadas a
   modelos, comandos y notas del caso, en orden). Un caso con
   `requirementsMet: false` puntúa 0 aunque el juez le diera buena nota. El paso
   termina cuando cada caso que la persona preguntó tiene su explicación.
7. **Exportar** solo si la persona lo decide, ya sabiendo qué cambió el
   candidato (paso 5), y el candidato tiene `exportable: true`, con el
   `export.cli` o `export.tool` del informe. Usa una carpeta nueva.
   El `manifest.json` exportado incluye la recomendación y los límites de este
   informe. El original no cambia; instalar la variante es otra decisión de la
   persona.

## Errores

Todos llegan como `{"error", "code"}`: `job-not-found`, `candidate-not-found`,
`too-many-candidates`, `case-not-found`, `invalid-request` o `invalid-arguments`. Corrige el id con
la lista o con `gepa job show <jobId>` y repite la consulta.
