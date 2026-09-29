---
name: setup-gepa
description: Instala, configura y comprueba el motor local GEPA para optimizar skills y políticas JEV. Úsala cuando la persona pida "configura GEPA", "prepara GEPA", "¿está listo GEPA?" o cuando otra operación GEPA falle por motor, proveedor, modelo, credencial o dependencia.
license: MIT
compatibility: Requiere Python 3.12+ y acceso a la terminal. Funciona en Windows y macOS. MCP es opcional.
metadata:
  version: "0.1.0"
  engine: "gepa==0.1.4"
---

# setup-gepa

Deja un motor GEPA utilizable o entrega un diagnóstico concreto de lo que falta.
No requiere que la persona conozca GEPA: tú traduces cada hallazgo a una acción.

## Recorrido mínimo

El agente instala, configura y comprueba. La persona elige proveedor/modelo y
carpetas, y entrega una credencial por un canal privado cuando haga falta.
Reutiliza las decisiones de la conversación y la configuración existente.
Agrupa las elecciones pendientes en una sola pregunta; presenta las carpetas
propuestas y permite aceptarlas juntas. Elige tú los identificadores de conexión,
el nombre de la variable y los comandos. No conviertas esos detalles ni cada
cambio reversible en otra pregunta de confirmación.

Si elige un solo modelo de chat, explica que lo usarás como ejecutor y reflexión
y configura ambos roles con esa conexión; respeta cualquier elección distinta.
Jev de TypeSafe solo toma decisiones: necesita un modelo de chat separado para
reflexión. Reúne ambas elecciones en la misma pregunta, sin preguntar por
endpoints ni protocolos; configura tú la interfaz correcta de cada conexión.
El juez se configura cuando la tarea lo requiere. Los modelos no vienen
prefijados: si prefiere decidirlos después, termina la instalación y señala
que la previsualización y la búsqueda necesitan esas conexiones.

Para OpenRouter en Windows, abre tú la ventana privada con `set-key --ui`:
la persona solo pega la clave y pulsa Guardar. Los detalles y alternativas están
en [connections.md](connections.md). No la envíes a abrir PowerShell para el
recorrido normal ni solicites la clave en la conversación.

## Cuándo usarla

- Primera vez que se usa GEPA en esta máquina o proyecto.
- Alguien pide revisar o cambiar modelos, conexiones o la carpeta de datos.
- Otra skill GEPA falló con un error de entorno.

## Cómo invocar el motor

Prefiere la tool nativa si el anfitrión la expone (servidor MCP `gepa`):
`gepa_setup_check` y `gepa_setup_show`. Si no hay tools, usa la CLI. Ambas
ejecutan la misma operación y devuelven el mismo JSON.

```bash
gepa setup check --json      # diagnóstico estructurado
gepa setup show --json       # configuración actual, sin credenciales
```

Para la CLI, usa el comando de `engine.md`, en la carpeta de esta skill: lo
escribió `gepa setup install` con el intérprete donde está instalado el motor y
la misma configuración que usan las tools MCP, así que funciona aunque `gepa` no
esté en el PATH. En estas tablas, `gepa` abrevia ese comando. Sin `engine.md`
(skill copiada a mano), usa `gepa` o `python -m gepa_engine` con el intérprete
donde se instaló el paquete `gepa-capability`.

## Archivos intermedios

Los archivos que escribes solo para trabajar con el motor van en
`<home>/solicitudes/`: las salidas que guardas para leerlas, los scripts de
apoyo y las copias de respaldo. `home` es la carpeta del motor, donde vive su
configuración: la dan `gepa setup check` y `gepa setup show`. Con la de por
defecto, la carpeta es `.gepa/solicitudes/` del proyecto. Créala si no existe y
da a cada archivo un nombre que diga qué es. Nunca los escribas en `/tmp`,
`$TMPDIR`, `$TEMP`, `%TEMP%`, `$env:TEMP` ni otra carpeta temporal del sistema:
quedarían fuera del proyecto y la persona no vería qué hiciste. Las demás skills
GEPA guardan ahí también sus solicitudes al motor.

## Procedimiento

1. **Inspeccionar e instalar.** Usa `engine.md` y `gepa setup show --json` para
   conocer lo existente, sin volver a preguntar decisiones ya tomadas. Si falta
   el motor, comprueba Python 3.12+ y el acceso al repositorio e instálalo tú en
   un entorno persistente con
   `uv tool install "git+https://github.com/PAAG-Ebicys/gepa-capability@main" --python 3.12`,
   o con pip dentro de un venv permanente. Una wheel o carpeta local aportada
   por la persona también sirve. Ejecuta `gepa setup install --host <anfitrión>`
   desde la raíz del proyecto. Listo cuando puedes invocar el motor y conoces
   su configuración; solo pide intervención si falta acceso o una decisión
   que no puedes resolver.
   Si la instalación no reconoce `set-key --ui` o `--protocol`, actualiza el
   paquete en ese entorno y vuelve a instalar las skills con el mismo ámbito;
   no intentes usar opciones nuevas con un motor antiguo. Respeta una revisión
   fijada por la persona y explica si hace falta actualizarla.
2. **Cerrar las elecciones pendientes juntas.** Pregunta solo por el proveedor,
   el modelo (o modelos si ya distingue roles) y la aceptación de las carpetas
   propuestas en `folders` de `setup show`. Para un servidor local necesitas
   además su URL. Si está todo elegido, pasa directamente al siguiente paso.
   Listo cuando puedes aplicar la configuración elegida o la persona decide
   dejar los modelos pendientes.
3. **Configurar sin delegar comandos.** Crea las conexiones y asigna los roles
   elegidos; designa las carpetas con `gepa setup folders` o sus opciones
   `--templates` y `--adapters`. Usa rutas del proyecto o las que haya indicado
   la persona. Para OpenRouter y otras conexiones con clave, reutiliza una
   credencial disponible o sigue [connections.md](connections.md): en Windows
   abre la ventana privada, espera su cierre y confirma que se guardó. Listo
   cuando las carpetas existen y las conexiones elegidas tienen credencial si
   la necesitan. Una cancelación deja ese paso pendiente; no abras otra ventana
   automáticamente.
4. **Comprobar una vez y resolver lo que bloquee.** Ejecuta
   `gepa setup check --json` después de configurar; puede enviar una petición
   pequeña a cada conexión asignada. Interpreta los hallazgos por su `code`:

   | code | Qué significa | Qué hacer |
   | --- | --- | --- |
   | `engine-missing` | Falta el paquete Python `gepa` | Ejecutar el `nextStep` (pip install con el mismo intérprete) |
   | `dependency-missing` | Falta un módulo Python o un comando declarado | Instalarlo según `nextStep`; si no es necesario para la tarea, explicarlo |
   | `data-dir-unwritable` | La carpeta de datos no se puede escribir | `gepa setup data-dir <ruta>` |
   | `credential-missing` | La conexión necesita una API key y no hay ninguna | Captura privada según [connections.md](connections.md); en Windows el agente abre `set-key --ui` |
   | `credential-rejected` | El proveedor devolvió 401/403 | Revisar o renovar la clave; mismo mecanismo que arriba |
   | `provider-unreachable` | No responde el servidor de modelos | Arrancar el servidor local o corregir la URL con `gepa setup connection add ... --url` |
   | `model-missing` | El modelo no está en el catálogo | Elegir uno de los listados en `nextStep` |
   | `inference-failed` | El modelo no respondió a la comprobación de ejecución | Comprobar la disponibilidad y el protocolo de la conexión según `nextStep` |
   | `protocol-incompatible` | Una conexión de decisiones se usó donde se necesita chat | Corregir la conexión o el rol tú: Jev decide, un modelo de chat reflexiona o puntúa rúbricas |
   | `role-unassigned` / `role-dangling` | Un rol global no apunta a una conexión válida | Si la operación actual requiere ese rol, asignarlo con `gepa setup role <rol> <id-conexión>` o indicar la conexión en esa operación; los demás roles pueden quedar pendientes |
   | `no-connections` | Aún no hay conexiones | Preparar las que requiera la próxima operación cuando se elijan sus modelos |
   | `folders-undesignated` | Las carpetas de plantillas y de adaptadores propios nunca se designaron (aviso: no bloquea) | Designar las carpetas ya elegidas con `gepa setup folders` |

   Repite el diagnóstico solo después de corregir un hallazgo. El diagnóstico global
   puede mostrar `ready: false` por roles que la operación actual no necesita:
   informa cuáles están pendientes y comprueba los roles requeridos al
   previsualizar o iniciar el trabajo. Los avisos (`warning`) no bloquean;
   explícalos y sigue.
   Listo cuando responde el motor y las conexiones necesarias; un juez no
   requerido o modelos aplazados no impiden dar por terminada la instalación.
5. **Entregar un cierre breve:** instalado, modelos/roles elegidos, carpeta de
   trabajo y próximo paso (`prepare-gepa-experiment`). Si falta algo, nombra
   solo ese bloqueo y su acción. Sin claves, dumps JSON ni lista de detalles
   técnicos. Las modificaciones de `.gitignore` protegen datos y credenciales;
   informa que se aplicaron sin pedir otra aprobación.

## Reglas

- Nunca escribas una API key en argumentos de comandos, archivos sin cifrar,
  registros ni en la conversación. Solo por variable de entorno o, en Windows,
  `set-key` (preferentemente su ventana `--ui`).
- No asumas rutas, servidores ni modelos: todo sale de `gepa setup show`.
- Los requisitos de un adaptador propio (un navegador, una API o la credencial de
  la tarea) los diagnostica `gepa adapter check`, como indica
  prepare-gepa-experiment.
- El adaptador JEV integrado admite el esquema de política del motor, no
  cualquier formato externo llamado JEV. El proveedor y el formato del
  artefacto son decisiones distintas. Para Jev de TypeSafe en OpenRouter usa
  una conexión `--protocol decisions`, solo como decisor de políticas choice;
  reflexión y juez necesitan chat. Identifica el formato real antes de
  preparar; prepare-gepa-experiment describe la comprobación de compatibilidad.
- `GEPA_HOME` (o `--home`) cambia dónde vive la configuración (por defecto
  `./.gepa` en el proyecto actual); `data-dir` o `GEPA_DATA_DIR` cambian dónde
  viven trabajos y resultados. Ambos son configurables.
- Las carpetas de plantillas y de adaptadores quedan fuera de la carpeta del
  motor, para que copiar una plantilla nunca copie una credencial. Si la
  carpeta del motor no se llama `.gepa` y la comparten varios proyectos, cada
  proyecto es la carpeta desde la que se ejecuta el motor: ejecútalo desde la
  raíz del proyecto.
- Instalar esta skill en otro anfitrión: `gepa setup install --host <claude-code|codex|opencode|pi|generic> [--scope user]`.
