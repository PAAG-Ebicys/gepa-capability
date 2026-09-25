# Casos para una Skill

Una Skill se prepara con `{"type": "skill", "path": "<carpeta con SKILL.md>"}`.
El motor lee todos los archivos de texto UTF-8 de la carpeta y omite los ocultos
(`.DS_Store`, `.git/`) y `__pycache__/`. Un archivo binario o un enlace simbólico
da `invalid-skill`: pide a la persona que lo quite o lo copie como archivo real.
GEPA reescribe solo `SKILL.md`, y el `name` de su frontmatter queda fijo.

## Cómo se ejecuta un caso

Cada caso corre en un espacio aislado nuevo: la Skill en `skill/` (solo
lectura) y los archivos del caso en la raíz. El modelo del rol `executor` carga
`SKILL.md` y actúa con cuatro acciones: `list_files`, `read_file`, `write_file` y
`final`. No tiene red ni ejecución de código. Una tarea que exige ejecutar
programas, un navegador o una API necesita un adaptador propio:
[new-adapter.md](new-adapter.md).

`executor` en la solicitud fija la sesión, y la aprobación la sella. Dónde va
la solicitud y cómo se escriben sus rutas: «Archivos intermedios» en
[SKILL.md](SKILL.md).

```json
{"artifact": {"type": "skill", "path": "<proyecto>/skills/mi-skill"}, "objective": "…",
 "inputs": [{"path": "<proyecto>/casos.json"}], "executor": {"maxTurns": 8, "maxTokensPerTurn": 4096}}
```

`maxTurns` va de 1 a 50 (por defecto 8) y `maxTokensPerTurn` de 256 a 32768
(por defecto 4096). Una respuesta cortada por ese límite puntúa como fallo del
caso: si la tarea exige escribir archivos largos, sube `maxTokensPerTurn`.

## Formato de un caso

Los casos de una Skill van en JSON (en línea o en un archivo `.json`), porque
`expected` es una lista de comprobaciones:

```json
{"id": "fila-1450", "split": "train",
 "input": {"prompt": "Armoniza input.csv y guarda el resultado en output.csv.",
           "files": {"input.csv": "patient_id,Creatinine\n1450,\"1,2\"\n"}},
 "expected": [{"type": "file-exists", "path": "output.csv"},
              {"type": "file-equals", "path": "output.csv", "text": "patient_id,Creatinine\n1450,1.20\n"}]}
```

`input` también puede ser solo el texto de la tarea. Las rutas son relativas,
con `/`, de hasta 150 caracteres (100 por nombre) y válidas en Windows y macOS:
sin unidad, sin `:`, `..` ni nombres como `NUL`. `skill/` está reservada para la
Skill.

| `type` | Campos | Se cumple cuando |
| --- | --- | --- |
| `answer-equals` | `text` | la respuesta final es ese texto |
| `answer-contains` | `text` | la respuesta final contiene el texto |
| `answer-matches` | `pattern` | una expresión regular encuentra coincidencia en la respuesta |
| `file-exists` / `file-absent` | `path` | el archivo existe / no existe al terminar |
| `file-equals` | `path`, `text` | el archivo tiene exactamente ese texto |
| `file-contains` | `path`, `text` | el archivo contiene el texto |
| `file-json-equals` | `path`, `value` | el archivo es JSON con ese valor exacto (`1` no es `true`) |

Los textos se comparan sin distinguir CRLF de LF ni espacios y saltos de línea
al final. Cada comprobación puede llevar `name`; por defecto se llama
`<type>:<path>`, y ese nombre es su submétrica. El motor añade a cada caso la
comprobación `finished` (la sesión terminó con `final`). `finished`, `judge` y
los nombres que empiezan por `rubric:` están reservados.

Cada comprobación puede llevar `"required": true`, que la convierte en un
**requisito obligatorio**. Qué hace la marca, qué comprobaciones marcar, dónde
escribir las marcas y qué decir a la persona: «Comprobaciones obligatorias» en
[SKILL.md](SKILL.md).

## Elegir el evaluador

`evaluator` en la solicitud elige cómo se puntúa un caso, y la aprobación lo
sella junto con su regla de agregación (`evaluator.aggregation.rule`):

- **`file-checks`** (por defecto, sin `evaluator`): el score es la fracción de
  comprobaciones cumplidas, o 0 si falla un requisito obligatorio. Recomiéndalo
  cuando el resultado se pueda verificar: formato, archivos, valores exactos o
  casos de un benchmark con la salida correcta.
- **`rubric-judge`**: un modelo juez puntúa cada criterio de una rúbrica de 0 a
  1, con una razón. Recomiéndalo cuando importe la calidad semántica y no haya
  una única respuesta correcta; combínalo con comprobaciones obligatorias para
  los requisitos duros.

```json
{"evaluator": {"name": "rubric-judge", "checksWeight": 0, "maxTokens": 1024,
               "rubric": [{"name": "fidelidad", "description": "Conserva los pacientes completos y sus valores"}]}}
```

- `rubric` es la rúbrica común; un caso puede añadir la suya en `rubric`, con
  otros nombres. Cada caso necesita al menos un criterio (hasta 20 en total), y
  `expected` pasa a ser opcional.
- El juez lee la tarea, los archivos del caso, la respuesta final, los archivos
  producidos y la rúbrica. El ejecutor nunca ve la rúbrica.
- Score del juez: la media de sus criterios. Score del caso:
  `(1 − checksWeight) × juez + checksWeight × fracción de comprobaciones`
  (`checksWeight` de 0 a 1, por defecto 0).
- Con juez, `finished` también es obligatorio: si una comprobación obligatoria
  o `finished` falla, el caso puntúa 0 aunque el juez dé 1.
- Submétricas: cada comprobación (sí/no), `judge` y `rubric:<criterio>`.
- El modelo es el rol `judge` de setup-gepa, u otra conexión con `--judge` en
  la previsualización y `models.judge` en el trabajo. `maxTokens` (256 a
  32768) limita cada veredicto.
- Un veredicto que no cumple el contrato se devuelve al juez con el problema,
  y uno cortado se pide otra vez: hasta 3 reintentos. Si ninguno sirve, la
  evaluación se detiene sin puntuar al candidato (`judge-invalid-output` o
  `judge-output-truncated`): cambia el modelo juez o sube `maxTokens`.

## Qué revisar con la persona

- En el resumen: `artifact.files` (lo que queda fijo), `executor`,
  `evaluator.description`, `evaluator.aggregation.rule` y, con juez,
  `evaluator.rubric`. No hay `coverage.expected`: cada caso tiene sus propias
  comprobaciones.
- En la previsualización, además de `output`, `score`, `submetrics` y
  `feedback`: `requirementsMet`, `checks` (qué se cumplió y un resumen de lo
  obtenido frente a lo esperado), `effects` (archivos creados o modificados),
  `session` (cómo terminó y sus acciones) y, con juez, `judge` (score y razón por
  criterio, y un resumen). Pregunta también si cada razón del juez coincide con
  el criterio de la persona. Un caso donde el original acierta con comprobaciones
  débiles, o donde el juez premia algo que la persona rechazaría, revela un
  `expected` o una rúbrica que no representan el objetivo.
