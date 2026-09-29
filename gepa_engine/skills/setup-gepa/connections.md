# Configurar conexiones sin exponer credenciales

Lee este archivo al conectar un proveedor o resolver un error de clave.
`gepa` abrevia el comando de `engine.md`; conserva su intérprete y `--home`.
El agente ejecuta los comandos, la persona solo aporta las decisiones y la
credencial mediante captura privada.

## Conexión elegida

```bash
gepa setup connection add --id <id> --provider openrouter --model <org/modelo>
gepa setup role executor <id>
gepa setup role reflection <id>
```

Los identificadores los elige el agente. Los nombres de modelo son los
elegidos por la persona, sin un modelo prefijado. Asigna solo los roles
acordados. Para un servidor local, cambia el proveedor a `local` e indica
`--url <url-/v1>`. El juez solo participa si la evaluación lo necesita.

## API key en Windows

Si `hasKey` es verdadero, reutiliza esa fuente; una nueva clave solo hace
falta cuando está ausente o el proveedor la rechaza. Para la captura normal:

```bash
gepa setup connection set-key <id> --ui
```

Si el decisor y la reflexión son conexiones distintas al mismo proveedor,
captura una sola vez: `gepa setup connection set-key <id-decisor> --also
<id-reflexión> --ui`. `--also` puede repetirse para otras conexiones del mismo
proveedor. Las conexiones deben existir antes de abrir la ventana.

Abre tú este comando mediante las herramientas del agente, sin enviar a la
persona a una terminal. Si el anfitrión necesita un proceso separado, lánzalo
con la ventana de terminal oculta y conserva la configuración del comando de
`engine.md`. La ventana de credencial sí debe ser visible: la persona pega con
Ctrl+V en el campo oculto y pulsa Guardar. La clave se cifra con DPAPI para esa
cuenta de Windows; no pasa por argumentos, chat ni resultados del agente.

Espera a que cierre la ventana y comprueba el resultado del proceso y
`gepa setup show --json` (`hasKey`); después ejecuta el diagnóstico. No generes
capturas de pantalla ni leas el portapapeles o el contenido del campo. Si
cancela, deja la conexión pendiente. Si la interfaz no está disponible,
explica el impedimento antes de ofrecer el método de terminal.

Una variable configurada con `--api-key-env` tiene prioridad sobre la clave
cifrada. Si una variable contiene una clave rechazada, guardar otra en la
ventana no la reemplaza. Para usar la nueva clave cifrada, vuelve a guardar la
misma conexión sin `--api-key-env`; conserva proveedor, modelo, URL, nombre y
`--protocol` (en Jev nativo, `--protocol decisions`).

## Otras plataformas o entornos sin interfaz

En macOS y Linux el almacén actual no conserva la clave entre procesos. Usa
una variable de entorno disponible para **el proceso que ejecutará GEPA** y
declara su nombre en la conexión:

```bash
gepa setup connection add --id <id> --provider openrouter --model <org/modelo> --api-key-env OPENROUTER_API_KEY
```

Reutiliza un mecanismo de secretos del anfitrión si existe. Si necesita una
intervención manual, pide únicamente esa acción privada y verifica después
que la credencial esté disponible, sin imprimir su valor. Una variable creada
en otra terminal no se hereda automáticamente por un agente o MCP ya iniciado.

`set-key` sin `--ui` conserva la entrada por stdin/getpass para usuarios que
prefieren terminal en Windows. Esa alternativa no es el recorrido normal.

## JEV con OpenRouter

Para **Jev de TypeSafe**, configura la conexión elegida por la persona con
`--provider openrouter --protocol decisions --model <modelo-elegido>`.
No lo pruebes con `chat/completions` ni lo asignes a reflexión: recibe
`state` y `questions` en la API Decisions, y devuelve decisiones tipadas.
El motor construye esa solicitud para una política choice y normaliza la
respuesta para puntuarla, conservando la evidencia nativa. La comprobación
de setup usa ese protocolo; el catálogo de modelos de chat no es una condición
de compatibilidad de Jev.

Si una instalación anterior guardó ese modelo como `chat`, actualiza la misma
conexión a `decisions`, conservando su ID y la fuente de credencial. Eso evita
volver a pedir la clave que ya está configurada.

`models.decider` apunta a esa conexión y `models.reflection` a una conexión de
chat. Un modelo local o de chat OpenRouter también puede ser decisor, con el
protocolo `chat` por defecto. No pidas al usuario elegir un protocolo técnico:
se deduce de la interfaz documentada del modelo que seleccionó.

La política sigue usando el esquema del motor. Un archivo de una implementación
externa puede necesitar normalización antes de preparar el experimento, según
la guía de formatos de prepare-gepa-experiment. Actualmente la integración
nativa optimiza preguntas **choice**; no declares soporte de noul o score.

Referencia: [Jev en OpenRouter](https://openrouter.ai/docs/guides/community/jev).
