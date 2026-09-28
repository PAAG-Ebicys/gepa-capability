# Instalar GEPA en Claude Code

Necesitas Claude Code, git y un motor GEPA instalado en un entorno Python persistente. La [guía general](install.md#1-instalar-el-motor) incluye alternativas a uv para Windows, macOS y Linux. Puedes instalar las skills antes de elegir los modelos del experimento.

## Instalar en un proyecto

Abre una terminal en la raíz del proyecto donde usarás Claude Code:

```bash
uv tool install "git+https://github.com/PAAG-Ebicys/gepa-capability@main" --python 3.12
gepa setup install --host claude-code
claude
```

Si el motor ya está instalado, empieza por `gepa setup install`. El comando copia las cuatro skills en `.claude/skills`, genera su `engine.md` con la ruta del Python instalado y añade `mcpServers.gepa` a `.mcp.json`, conservando los otros servidores. Esa entrada apunta a la configuración `.gepa` de este proyecto.

Las rutas del intérprete son propias de esta máquina. Al compartir el proyecto con otra persona, debe ejecutar el instalador en su equipo para generar sus rutas.

En una sesión nueva de Claude Code:

1. Acepta el servidor MCP `gepa` del proyecto cuando Claude Code solicite autorización.
2. Ejecuta `/mcp` y comprueba que `gepa` esté conectado.
3. Ejecuta `/setup-gepa` para comprobar el motor, las carpetas y las conexiones que necesites.

Las otras skills se invocan con `/prepare-gepa-experiment`, `/optimize-with-gepa` y `/review-gepa-results`. Empieza por preparar y aprobar los casos; después inicia la optimización. [Ver qué hace cada paso](flujo.md).

También puedes pedirle a Claude Code:

> Instala GEPA Capability desde https://github.com/PAAG-Ebicys/gepa-capability en este proyecto. Verifica el acceso al repositorio, instala el motor en un entorno Python persistente y ejecuta `gepa setup install --host claude-code`. Comprueba las skills y MCP; podemos elegir los modelos después.

## Instalar para todos tus proyectos

```bash
gepa setup install --host claude-code --scope user
```

Esto copia las skills a `~/.claude/skills`. Para MCP, el instalador **imprime** un comando `claude mcp add --scope user gepa -- …`: ejecuta el comando completo que devuelve, porque contiene la ruta real del Python instalado. No escribe automáticamente la configuración MCP de usuario.

Sin una configuración explícita, cada proyecto conserva su propia `.gepa`. Si quieres compartir conexiones, datasets y trabajos entre proyectos, elige una carpeta permanente y ejecuta:

```bash
gepa --home "<carpeta-gepa-compartida>" setup install --host claude-code --scope user
```

Sustituye el marcador por una ruta real y ejecuta el comando MCP que esta instalación imprima. Reinicia Claude Code y comprueba `/mcp` y `/setup-gepa` desde el proyecto en el que trabajarás.

## Comprobar o resolver problemas

- Si una skill no aparece, inicia una sesión nueva desde la raíz correcta y comprueba sus archivos en `.claude/skills` o `~/.claude/skills`.
- Si MCP no conecta, revisa `/mcp` y la ruta del intérprete en `.mcp.json`. El entorno Python debe seguir existiendo.
- Si prefieres usar solo la CLI, instala con `gepa setup install --host claude-code --no-mcp`. Las skills usan el comando registrado en su `engine.md`.
- Si `gepa setup check --json` devuelve `ready: false` por roles pendientes, configura las conexiones que requiera la próxima operación. Instalar las skills no exige elegir modelos de antemano.

Referencias de Claude Code: [skills](https://code.claude.com/docs/en/skills) y [servidores MCP](https://code.claude.com/docs/en/mcp).
