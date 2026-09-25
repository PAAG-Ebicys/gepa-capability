# GEPA Capability

GEPA Capability ayuda a mejorar **instrucciones**, no los pesos de un modelo. Toma una Skill o una política de decisión JEV, la ejecuta sobre casos aprobados, propone variantes de su texto y compara los resultados con el original. El resultado es una recomendación con evidencia; el original no se reemplaza automáticamente.

Se usa desde un agente que pueda leer skills y ejecutar comandos, como Codex, Claude Code, OpenCode o Pi. El paquete `gepa-capability` aporta el motor Python, la CLI `gepa` y un servidor MCP opcional. Las cuatro skills guían al agente por el recorrido:

| Skill | Para qué sirve | Resultado |
| --- | --- | --- |
| `setup-gepa` | Instalar o diagnosticar el motor y las conexiones a modelos | Entorno comprobado |
| `prepare-gepa-experiment` | Definir el original, los casos y cómo se puntúan; previsualizar y pedir aprobación | Dataset aprobado `ds-…` |
| `optimize-with-gepa` | Elegir los modelos y límites de **esta corrida**, buscar variantes y seguir el trabajo | Trabajo `job-…` con candidatos evaluados |
| `review-gepa-results` | Comparar original y candidatos, examinar casos y decidir si exportar | Recomendación y, si se solicita, una copia exportada |

## Cómo funciona

```text
Original + casos con respuesta esperada
              │
              ▼
 Preparar → previsualizar → aprobar el dataset
              │
              ▼
 Elegir conexiones y límites → búsqueda GEPA
              │
              ▼
 Revisar evidencia → conservar o exportar una variante
```

Los casos se separan en **entrenamiento**, **validación** y **prueba reservada**. El entrenamiento da ejemplos y feedback a la búsqueda; la validación sirve para comparar y seleccionar candidatos; la prueba se guarda para una comprobación final independiente. Una propuesta puede fallar en una muestra pequeña y ser descartada antes de la validación completa. Por eso «una iteración de búsqueda» no equivale a «un candidato validado».

Por ejemplo, una política puede recibir «pon una alarma a las cinco» y elegir `alarm_set` entre varias intenciones. Cada caso guarda la respuesta esperada fuera de la entrada del ejecutor. GEPA puede reescribir las instrucciones y las descripciones de las opciones, pero conserva los identificadores de las opciones. En una Skill, puede reescribir su `SKILL.md`; los demás recursos quedan fijos. [Ver el recorrido con más detalle](docs/flujo.md).

## Modelos y límites

Los nombres de modelos no vienen prefijados. Se crean **conexiones** a los modelos disponibles y se asignan según la tarea:

- **Ejecutor:** realiza cada caso con el texto original o candidato.
- **Reflexión:** examina el feedback y propone cambios de texto.
- **Juez:** puntúa una rúbrica cuando el evaluador del dataset lo requiere. Con una respuesta exacta o comprobaciones deterministas puede no hacer falta.

Una corrida puede elegir conexiones concretas en `models` sin cambiar las asignaciones generales. También admite límites de tiempo, evaluaciones e iteraciones de búsqueda (`maxProposals`, nombre histórico) y una meta opcional `validationScoreTarget` para la puntuación media de validación. La meta no se mide en la prueba reservada y **no siempre representa porcentaje de aciertos**: depende del evaluador aprobado. [Ver ejemplos y condiciones de parada](docs/flujo.md#límites-de-una-corrida).

## Instalación rápida

Requiere Python 3.12+, git y acceso al repositorio. El paquete se instala desde GitHub en un entorno persistente; las skills se copian después al proyecto donde trabajará el agente. Con [uv](https://docs.astral.sh/uv/):

```bash
uv tool install "git+https://github.com/PAAG-Ebicys/gepa-capability@main" --python 3.12
gepa setup install --host codex
gepa setup check --json
```

`@main` instala el código actual de la rama principal. El tag `@v0.1.0` fija la versión anterior y puede no incluir estas funciones. Para una instalación reproducible, sustituye `main` por el SHA de un commit probado. Ejecuta `setup install` desde la raíz del proyecto. Sustituye `codex` por `claude-code`, `opencode`, `pi` o `generic` según el agente. Si no tienes `uv`, o quieres conocer la instalación con wheel, el registro MCP y la resolución de errores, sigue la [guía de instalación](docs/install.md). Si GitHub responde `Repository not found`, comprueba el acceso de tu cuenta al repositorio.

También puedes pedirle al agente, desde la raíz del proyecto: «Instala GEPA Capability desde este repositorio, verifica el acceso, usa un entorno Python persistente y ejecuta `gepa setup install` para mi anfitrión». Si falta acceso a un repositorio privado, deberá indicártelo antes de continuar.

Tras instalar, recarga el agente y pide «configura GEPA». Los modelos concretos de una corrida se eligen al iniciarla; la previsualización del dataset ya necesita un ejecutor disponible. El diagnóstico global `gepa setup check` todavía marca `ready: false` hasta que los tres roles generales tengan conexiones, aunque un trabajo puede necesitar solo un subconjunto de esos roles.

## Qué guarda y qué no cambia

El motor guarda datasets, trabajos, métricas, trazas y un manifiesto de los modelos y límites usados. La exportación crea una carpeta nueva con la variante y su evidencia. **Aprobar un dataset, iniciar una búsqueda y exportar son pasos distintos**; ninguna búsqueda instala por sí sola la variante en tu proyecto.

La CLI y el servidor MCP llaman al mismo motor. Si tu agente no ofrece las tools MCP, puede usar la CLI descrita por el `engine.md` que genera la instalación de las skills.
