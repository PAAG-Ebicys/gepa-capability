# Compatibilidad de formatos JEV

El nombre JEV no identifica por sí solo un esquema. El agente debe leer el
artefacto y la implementación o documentación que lo ejecuta antes de elegir
un adaptador. Busca esa información en el proyecto y la conversación; pide
un archivo o referencia solo si no está disponible. La persona no necesita
elegir un protocolo ni escribir el adaptador.

## Política integrada del motor

El original admite `question`, `type`, `instructions` y `criteria`:

```json
{
  "question": "intent",
  "type": "choice",
  "instructions": "Elige la intención que corresponde a la entrada.",
  "criteria": {
    "alarm_set": "Crear una alarma",
    "calendar_add": "Crear un evento de calendario"
  }
}
```

`question` es un identificador, `criteria` asocia IDs con descripciones y el
decisor devuelve exactamente `{"choice":"<id>"}`. Un ejecutor que devuelve
otro esquema o Markdown no cumple ese contrato. Los IDs y su orden quedan
fijos; solo cambian instrucciones y descripciones.

Este formato usa el adaptador integrado. OpenRouter es una conexión al modelo,
no una variante del esquema de política.

## Jev de TypeSafe en OpenRouter

El modelo Jev real usa la API Decisions, no chat. Para una política choice
normalizada, el motor envía `state` y `questions` y lee
`answers[question].choice`; no exige que Jev genere texto con `{"choice":…}`.
Configura su conexión con `--protocol decisions` y usa un modelo de chat
separado para reflexión. La persona elige modelos; el agente configura el
protocolo. El adaptador integrado hace este recorrido, sin escribir otro
adaptador ni pedir al usuario resolver la compatibilidad.

El soporte nativo se limita a **choice**. Si el artefacto contiene un objeto
`questions` con otras primitivas o varias preguntas, identifica primero qué
decisión y superficie se pretende optimizar; no elimines otras preguntas
silenciosamente. Una solicitud completa de TypeSafe/OpenRouter no es el
archivo de política interno: separa estado/modelo de las instrucciones y
criterios que sí se optimizan.

Referencias: [Jev](https://openrouter.ai/docs/guides/community/jev) y
[esquema Decisions](https://openrouter.ai/docs/api/api-reference/alphadecisions/submit-a-decisions-request).

## Artefacto de otra implementación

1. Lee su esquema de entrada, el contrato de salida y cómo ejecuta la decisión.
   Conserva el original y sus recursos; identifica qué texto puede optimizarse.
2. Comprueba si existe una conversión sin pérdida al esquema del motor. Si la
   hay, prepara una copia normalizada y una correspondencia explícita de
   campos/IDs. Explica en una frase que el experimento usará esa copia, sin
   sustituir el original. Si no la hay, informa la incompatibilidad concreta;
   no declares que el adaptador integrado soporta el archivo.
3. Si el comportamiento depende de un ejecutor o evaluador externo, prepara tú
   un adaptador según [new-adapter.md](new-adapter.md). Un adaptador propio de
   tipo `jev-policy` también necesita el esquema de política del motor: no
   permite omitir la normalización. Conserva los campos fijos y los recursos
   que necesita la implementación externa.
4. Verifica el recorrido con un caso pequeño antes de preparar el dataset:
   candidato normalizado → entrada nativa → ejecución → salida observada →
   puntuación. Compara con la implementación original para detectar pérdida
   de campos o cambios de significado. Un error de esquema se corrige en la
   conversión o el adaptador; no se puntúa como un error semántico del modelo.
5. Previsualiza y pide la aprobación habitual del dataset. Para usar un
   candidato fuera de GEPA, conviértelo de vuelta al formato nativo en una
   carpeta nueva y comprueba su contrato allí; la exportación estándar del
   motor conserva su propio formato, no hace esa conversión automáticamente.

Si no conoces la implementación externa, puedes terminar setup-gepa. Explica
que la instalación está lista y que la compatibilidad de ese artefacto queda
pendiente; evita presentar un ejemplo con el formato interno como prueba de
compatibilidad del formato externo.
