# Recorrido: del ejemplo a la decisión

Esta guía explica las cuatro skills desde la perspectiva de quien prepara un experimento. Para instalar el motor, ve a [Instalación](install.md).

## 1. Configurar lo necesario

`setup-gepa` comprueba el motor, las carpetas de trabajo y las conexiones. Una **conexión** identifica un proveedor y un modelo; un **rol** indica qué hará ese modelo. El ejecutor resuelve casos, la reflexión propone cambios y el juez solo participa cuando la puntuación necesita una rúbrica. Los modelos son elecciones del experimento, no nombres incorporados a las skills. Se pueden asignar roles por defecto y sobrescribir las conexiones en un trabajo concreto.

## 2. Preparar y aprobar el dataset

`prepare-gepa-experiment` pide el artefacto original, un objetivo, casos con procedencia y un evaluador. En una política JEV de tipo `choice`, un caso puede verse así:

| Entrada entregada al ejecutor | Respuesta esperada, guardada aparte |
| --- | --- |
| «pon una alarma a las cinco» | `alarm_set` |

El ejecutor ve la entrada, la pregunta y las opciones de la política; **no ve la respuesta esperada**. El evaluador compara su elección con esa respuesta. Una Skill puede usar comprobaciones de archivos o una rúbrica, según la tarea. El agente presenta recuentos, procedencia, criterios y una previsualización del original; la persona aprueba explícitamente la versión que quiere usar. La aprobación devuelve un ID `ds-…`.

| Partición | Papel | ¿Puede guiar la búsqueda? |
| --- | --- | --- |
| `train` | Ejemplos y feedback para proponer cambios | Sí |
| `val` | Comparar candidatos y seleccionar el mejor | Sí, para selección |
| `test` | Comprobación independiente al final | No |

Conviene revisar con cuidado las respuestas ambiguas antes de aprobar. Un modelo pequeño puede ayudar a **marcar dudas**, pero no debería modificar silenciosamente las respuestas esperadas. El dataset registra su procedencia y la aprobación no significa que todas las filas se hayan revisado una por una.

Durante la preparación se pueden anotar preferencias de presupuesto o de meta de validación. No cambian los casos sellados: se confirman al crear cada trabajo y pueden ser distintas en otra corrida del mismo dataset.

## 3. Optimizar

`optimize-with-gepa` toma el `ds-…` y el mismo original que se previsualizó. Antes de lanzar, resuelve las conexiones que este trabajo necesita y los límites elegidos. Un ejemplo de especificación para una política JEV:

```json
{
  "name": "Intenciones",
  "artifact": {"type": "jev-policy", "path": "<ruta-al-proyecto>/policy.json"},
  "dataset": "ds-…",
  "models": {"decider": "conexion-ejecutor", "reflection": "conexion-reflexion"},
  "limits": {
    "maxMetricCalls": 120,
    "maxProposals": 5,
    "timeLimitMinutes": 60,
    "validationScoreTarget": 0.8
  }
}
```

`models` elige **IDs de conexiones ya configuradas**, no nombres de modelos. Puede omitirse para usar los roles por defecto. Una Skill usa `models.executor`; `models.judge` se usa si el evaluador aprobado necesita juez. Un adaptador propio declara los roles necesarios para esa tarea. El manifiesto del trabajo congela qué conexiones, modelos y límites se usaron.

La búsqueda se puede entender como esta secuencia:

```text
original → propuesta generada → prueba en una muestra de train
                                  ├─ no mejora: rechazada
                                  └─ mejora: validación completa → candidata
```

`maxProposals` conserva su nombre de la API, pero cuenta **iteraciones de búsqueda**, no propuestas individuales ni candidatos exitosos. Una iteración cuyas propuestas no superan la muestra puede agotar el límite sin producir candidato validado. El trabajo puede terminar con la recomendación de conservar el original. Si no hay candidato que haya mejorado la validación completa, la prueba reservada se omite para mantenerla disponible para otra búsqueda.

### Límites de una corrida

- `timeLimitMinutes`: tiempo máximo por intento de ejecución, incluida la finalización que alcance a realizar.
- `maxMetricCalls`: presupuesto de evaluaciones durante la búsqueda. La prueba final tiene su propio consumo, visible por separado.
- `maxProposals`: máximo de iteraciones. Varias pueden ser rechazadas en la muestra y no llegar a validación completa.
- `validationScoreTarget`: meta opcional entre 0 y 1 para la **mejor puntuación media en validación completa**. Si el original ya la cumple, la búsqueda puede detenerse antes de proponer cambios.

La puntuación no siempre es exactitud binaria: depende del evaluador del dataset. Solo en un evaluador de 0/1 por caso, una meta de `0.8` significa 80 % de aciertos; con 48 casos, hacen falta al menos 39. La meta usa validación, nunca la prueba reservada. El límite de tiempo puede impedir alcanzarla; la meta puede detener una corrida antes de agotar el tiempo.

## 4. Revisar y exportar

`review-gepa-results` lee la evidencia guardada **sin volver a llamar modelos**. Muestra qué propuestas llegaron a validación, qué cambió en las instrucciones, qué casos mejoraron o empeoraron, cuánto se gastó y si el test estaba intacto. Distingue un trabajo incompleto de uno cuyo original realmente ganó. Un score parcial no se convierte en un porcentaje completo.

La recomendación considera primero el candidato elegido por validación y después la comprobación reservada, si llegó a realizarse. La persona puede conservar el original o pedir la exportación de una variante. Exportar crea una carpeta nueva con artefacto, manifiesto y evidencia; no instala ni sobrescribe el original.
