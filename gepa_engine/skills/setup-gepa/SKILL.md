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

1. **Comprobar.** Ejecuta `gepa setup check --json`. Si no hay `engine.md` ni
   comando `gepa`, el motor no está instalado en este entorno. `gepa-capability`
   no está publicado en PyPI: pide a la persona la wheel
   (`gepa_capability-<versión>-py3-none-any.whl`) o la carpeta del paquete e
   instálala con Python 3.12 o posterior (`uv tool install <wheel>`, o
   `python -m pip install <wheel>` dentro de un venv). Después, `gepa setup
   install --host <anfitrión>` en la raíz del proyecto, recarga el agente y repite.
2. **Interpretar cada hallazgo** por su `code`, no por el texto:

   | code | Qué significa | Qué hacer |
   | --- | --- | --- |
   | `engine-missing` | Falta el paquete Python `gepa` | Ejecutar el `nextStep` (pip install con el mismo intérprete) |
   | `dependency-missing` | Falta un módulo Python o un comando declarado | Instalarlo según `nextStep`; si no es necesario para la tarea, explicarlo |
   | `data-dir-unwritable` | La carpeta de datos no se puede escribir | `gepa setup data-dir <ruta>` |
   | `credential-missing` | La conexión necesita una API key y no hay ninguna | Seguir `nextStep`: pedir a la persona que defina la variable de entorno indicada o, solo en Windows, que ejecute `gepa setup connection set-key <id>` (la clave se escribe por stdin; nunca la pongas en argumentos ni la repitas en el chat). En macOS y Linux `set-key` no conserva la clave: usa la variable y `--api-key-env` |
   | `credential-rejected` | El proveedor devolvió 401/403 | Revisar o renovar la clave; mismo mecanismo que arriba |
   | `provider-unreachable` | No responde el servidor de modelos | Arrancar el servidor local o corregir la URL con `gepa setup connection add ... --url` |
   | `model-missing` | El modelo no está en el catálogo | Elegir uno de los listados en `nextStep` |
   | `inference-failed` | El modelo aparece en el catálogo pero no respondió a una ejecución pequeña | Cargar el modelo o revisar el endpoint de chat según `nextStep` |
   | `role-unassigned` / `role-dangling` | Un rol global no apunta a una conexión válida | Si la operación actual requiere ese rol, asignarlo con `gepa setup role <rol> <id-conexión>` o indicar la conexión en esa operación; los demás roles pueden quedar pendientes |
   | `no-connections` | Aún no hay conexiones | Preparar las que requiera la próxima operación cuando se elijan sus modelos |
   | `folders-undesignated` | Las carpetas de plantillas y de adaptadores propios nunca se designaron (aviso: no bloquea) | Designarlas en el paso 4 con `gepa setup folders` |

3. **Configurar las conexiones necesarias para la operación actual.** Pregunta
   qué modelo quiere para cada rol que esa operación usa: `executor`/`decider`
   ejecuta los casos, `reflection` propone cambios durante la búsqueda y
   `judge` puntúa solo si el evaluador lo requiere. Se puede dejar pendiente la
   elección de modelos hasta previsualizar o iniciar un trabajo. Una conexión
   puede asignarse como rol global o indicarse en la operación concreta; muestra
   cada cambio antes de aplicarlo. Ejemplos con identificadores elegidos por la
   persona:

   ```bash
   gepa setup init --data-dir <ruta>            # opcional; por defecto GEPA_HOME/data
   gepa setup connection add --id <id-local> --provider local --url <url> --model <modelo>
   gepa setup connection add --id <id-remoto> --provider openrouter --model <org/modelo> --api-key-env <VARIABLE>
   gepa setup role <rol-necesario> <id-conexión>
   ```

4. **Designar las carpetas del proyecto** donde van las plantillas de
   experimento y los adaptadores propios. Lee las propuestas en `folders` de
   `gepa setup show --json` (por defecto `gepa/plantillas/` y
   `gepa/adaptadores/` en la raíz del proyecto) y pregunta a la persona si le
   sirven. Con un «sí», ejecuta `gepa setup folders`. Si da otras, ejecuta
   `gepa setup folders --templates <ruta> --adapters <ruta>`, con rutas
   relativas a la raíz del proyecto o absolutas. El comando crea las carpetas
   y añade al `.gitignore` de la raíz del proyecto solo las reglas que faltan,
   sin tocar las de la persona: git no guarda `.gepa/` (la base de datos y las
   credenciales cifradas), las plantillas ni los adaptadores, pero sí las
   copias para compartir (`<nombre>.compartir/`). Una carpeta fuera del
   proyecto no recibe regla. Dile qué reglas añadió (`gitignore.added`). Listo
   cuando `folders.designated` es `true` en `gepa setup show`.
5. **Repetir `gepa setup check`** después de los cambios. El diagnóstico global
   puede mostrar `ready: false` por roles que la operación actual no necesita:
   informa cuáles están pendientes y comprueba los roles requeridos al
   previsualizar o iniciar el trabajo. Los avisos (`warning`) no bloquean;
   explícalos y sigue.
6. **Resumir** a la persona: qué quedó listo, qué modelos elegidos usará cada rol, dónde
   se guardan los datos y cuáles son las carpetas de plantillas y de
   adaptadores. No incluyas credenciales ni valores de variables de entorno en
   el resumen.

## Reglas

- Nunca escribas una API key en argumentos de comandos, archivos del proyecto,
  registros ni en la conversación. Solo por variable de entorno o, en Windows, `set-key`.
- No asumas rutas, servidores ni modelos: todo sale de `gepa setup show`.
- Los requisitos de un adaptador propio (un navegador, una API o la credencial de
  la tarea) los diagnostica `gepa adapter check`, como indica
  prepare-gepa-experiment.
- `GEPA_HOME` (o `--home`) cambia dónde vive la configuración (por defecto
  `./.gepa` en el proyecto actual); `data-dir` o `GEPA_DATA_DIR` cambian dónde
  viven trabajos y resultados. Ambos son configurables.
- Las carpetas de plantillas y de adaptadores quedan fuera de la carpeta del
  motor, para que copiar una plantilla nunca copie una credencial. Si la
  carpeta del motor no se llama `.gepa` y la comparten varios proyectos, cada
  proyecto es la carpeta desde la que se ejecuta el motor: ejecútalo desde la
  raíz del proyecto.
- Instalar esta skill en otro anfitrión: `gepa setup install --host <claude-code|codex|opencode|pi|generic> [--scope user]`.
