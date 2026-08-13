# ChatGPT Export Archiver

Idioma: [English](README.md) | [简体中文](README.zh-CN.md) | [繁體中文（臺灣）](README.zh-TW.md) | [日本語](README.ja-JP.md) | [Español](README.es-ES.md)

Convierte un ZIP oficial de exportación de ChatGPT en un archivo SQLite privado y buscable. Impórtalo desde la CLI o la Web UI local, lee y busca conversaciones en el navegador y expórtalas como Markdown o TXT. Todo se ejecuta en tu equipo.

`Local primero` · `SQLite` · `Búsqueda privada` · `CLI` · `Web UI` · `Exportación Markdown/TXT`

## Por Qué Usarlo

- **Privado y local.** Los ZIP, bases de datos, exportaciones, subidas temporales, la Web UI y los logs permanecen en tu equipo.
- **Importación directa e incremental.** Lee ZIP oficiales o fragmentos JSON extraídos sin unirlos y actualiza un archivo existente con seguridad.
- **Lectura y búsqueda cómodas.** Usa el lector tipo chat, el diseño clásico, filtros, frases, exclusiones, paginación e índices rápidos opcionales.
- **Salida portátil.** Exporta Markdown o TXT deterministas para copias de seguridad, uso sin conexión o migración.

## Captura

Las capturas deben usar conversaciones sintéticas y no mostrar títulos, mensajes, nombres de archivo, correos ni rutas locales reales.

## Qué Hace Este Proyecto

- Importa `conversations.json` y shards `conversations-*.json` desde un ZIP exportado por OpenAI / ChatGPT, un archivo `conversations.json` suelto o un directorio ya extraído.
- Conserva metadatos de conversaciones, mapping nodes, roles de mensajes, texto, marcas de tiempo, enlaces de padres, seguimiento de origen y advertencias de importación.
- Admite importación incremental. Al reimportar un export más reciente en la misma base de datos, actualiza las conversaciones que cambiaron sin duplicar de forma intencionada los datos sin cambios.
- Crea un índice opcional FTS5 para búsqueda por CLI.
- Crea índices Web opcionales de búsqueda por subcadenas para acelerar la búsqueda en el navegador.
- Exporta conversaciones como Markdown, TXT o ambos.
- Incluye `verify`, `stats` e `inspect`, con una salida prudente que no imprime el texto de los chats.
- Incluye una Web UI local que puede arrancar aunque todavía no exista una base de datos y permite importar ZIP desde el navegador.
- Separa los logs de la salida estructurada de comandos y evita registrar títulos, snippets, raw JSON o cuerpos de mensajes.

## Privacidad

Todo se ejecuta en local y la Web UI se enlaza a `127.0.0.1` por defecto. Las importaciones normales no eliminan el ZIP original; las subidas del navegador usan una copia temporal del servidor. La base de datos y las exportaciones contienen conversaciones privadas, así que guárdalas de forma segura.

## Requisitos

- Python 3.10 o posterior; las dependencias Web se prueban con Python 3.13.
- SQLite con JSON. FTS5 es opcional porque la búsqueda por escaneo sigue disponible.
- Node.js y npm solo son necesarios para reconstruir o probar la Web UI de React.
- La CLI usa la biblioteca estándar de Python. Instala `requirements-web.txt` para ejecutar la Web UI y subir ZIP desde el navegador.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements-web.txt -c constraints-web-py313.txt
```

En Windows PowerShell:

```bash
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -r requirements-web.txt -c constraints-web-py313.txt
```

En Windows cmd.exe:

```bash
py -3 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install -U pip
python -m pip install -r requirements-web.txt -c constraints-web-py313.txt
```

## Inicio rápido

Coloca el ZIP exportado por ChatGPT fuera del repositorio y ejecuta el comando de importación seguro más rápido. Omite el hash de entrada y reconstruye FTS una sola vez al final, lo que suele ser mucho más rápido en archivos grandes que mantener FTS fila por fila.

```bash
NEW_ZIP="$HOME/Downloads/chatgpt_export/chatgpt_export.zip"
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

Equivalente en Windows PowerShell:

```bash
$env:NEW_ZIP = "$env:USERPROFILE\Downloads\chatgpt-export.zip"
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$env:NEW_ZIP" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

Equivalente en Windows cmd.exe:

```bash
set NEW_ZIP=%USERPROFILE%\Downloads\chatgpt-export.zip
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "%NEW_ZIP%" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

Arranca la Web UI local:

```bash
python chatgpt_archive.py web --db archive/chatgpt_archive.db --port 8787
```

Si todavía no existe una base de datos, la Web UI arranca igualmente y muestra un estado vacío con un panel de importación. Puedes elegir un ZIP exportado por ChatGPT en el navegador; el backend escribe una copia temporal local, lo importa y luego ejecuta automáticamente `verify`, `stats` y `web-index`.

```bash
python chatgpt_archive.py web --port 8787
```

## Flujo CLI habitual

Inspecciona un export sin imprimir contenido de chat:

```bash
python chatgpt_archive.py inspect --input "$NEW_ZIP"
```

Crea explícitamente una base de datos vacía:

```bash
python chatgpt_archive.py init --db archive/chatgpt_archive.db
```

Importa con la ruta recomendada para archivos grandes:

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
```

`--input` puede apuntar al ZIP oficial exportado, a un `conversations.json` suelto o a un directorio exportado ya extraído. Los directorios extraídos pueden contener `conversations.json` o archivos sharded `conversations-*.json`; no unas shards manualmente.

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input conversations.json --no-input-sha256 --rebuild-fts
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input ./extracted-export/ --no-input-sha256 --rebuild-fts
```

Verifica la coherencia estructural:

```bash
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
```

Muestra recuentos estructurados y límites temporales:

```bash
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

Busca texto de mensajes mediante la ruta de búsqueda de CLI. Imprime conversation IDs, node IDs y roles, no snippets:

```bash
python chatgpt_archive.py search --db archive/chatgpt_archive.db --limit 20 "python sqlite"
```

Exporta la conversación como Markdown, TXT o ambos formatos en la misma ejecución. `--format md` escribe archivos de cuerpo Markdown y actualiza el manifest, `--format txt` escribe archivos de cuerpo en plain text y actualiza el manifest, y `--format all` escribe ambos formatos de cuerpo y actualiza el manifest:

```bash
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format md --out exports
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format txt --out exports
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format all --out exports
```

Exporta un rango de fechas y reescribe archivos existentes si hace falta. Los límites de fecha de `--from` y `--to` solo aceptan `YYYY-MM-DD`:

```bash
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format md --out exports --from 2024-01-01 --to 2024-12-31 --force
```

Reconstruye los índices opcionales de búsqueda Web:

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
```

## Modos de importación

El comando recomendado para archivos grandes es:

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
```

Si quieres que SQLite dedique tiempo adicional a ordenar estadísticas del planner y el índice FTS después de importar, usa:

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts --optimize-after-import --optimize-fts-after-import
```

`--delete-input-on-success` se conserva por compatibilidad, pero no está disponible y falla antes de importar. Las importaciones normales no borran el ZIP original; elimínalo tú solo después de verificar la base de datos.

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts --delete-input-on-success
```

## Flujo de la Web UI

Usa el comando de inicio Web de la sección de instalación. La página puede abrir una base de datos existente o arrancar sin ella e importar un ZIP, con diseños chat y clásico paginados, búsqueda, controles de mensajes internos, copia y descarga. Las coincidencias muy largas recuperadas de datos de exportación antiguos también se pueden abrir y copiar completas; si caduca esa sesión temporal de búsqueda, vuelve a buscar. Atrás y Adelante restauran búsquedas enviadas, filtros, rutas, conversaciones seleccionadas y diseño sin añadir una entrada por cada tecla.

## Límites de seguridad de subida Web

Las subidas del navegador se comprueban antes de importar. El loopback usa los valores locales grandes de la tabla. El acceso no loopback debe habilitarse expresamente y usa valores seguros menores salvo que elijas el perfil local de confianza. CLI `import` no está sujeto a estos límites.

| Variable de entorno | Valor local por defecto | Controla |
|---|---|---|
| `CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES` | 20 GiB | Tamaño total del ZIP comprimido subido |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_JSON_MEMBER_BYTES` | 64 GiB | Tamaño máx. sin comprimir de un solo miembro JSON |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_JSON_MEMBERS` | 5,000 | Número máx. de miembros JSON de conversación |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_TOTAL_UNCOMPRESSED_BYTES` | 128 GiB | Total máx. de datos JSON sin comprimir |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_COMPRESSION_RATIO` | 1,000.0 | Ratio máx. de compresión para miembros JSON grandes |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_TOTAL_MEMBERS` | 100,000 | Número máx. total de miembros ZIP |
| `CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE` | unset | Ponlo en `local` solo en redes no loopback de confianza para usar valores locales por defecto en límites no definidos |

Para aumentar un límite local para un archivo legítimo grande, define la variable correspondiente antes de iniciar la Web UI:

```bash
# macOS / Linux
export CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES=64424509440  # 60 GiB
python chatgpt_archive.py web --port 8787
```

```powershell
# Windows PowerShell
$env:CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES = 64424509440  # 60 GiB
python chatgpt_archive.py web --port 8787
```

```batch
:: Windows cmd.exe
set CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES=64424509440
python chatgpt_archive.py web --port 8787
```

Para permitir un límite explícito grande de ZIP comprimido en una red interna de confianza y mantener otros límites no definidos en remote-safe:

```bash
# macOS / Linux
export CHATGPT_ARCHIVE_ALLOW_REMOTE_UPLOADS=true
export CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES=10737418240  # 10 GiB
python chatgpt_archive.py web --host 0.0.0.0 --port 8787
```

```powershell
# Windows PowerShell
$env:CHATGPT_ARCHIVE_ALLOW_REMOTE_UPLOADS = "true"
$env:CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES = 10737418240  # 10 GiB
python chatgpt_archive.py web --host 0.0.0.0 --port 8787
```

```batch
:: Windows cmd.exe
set CHATGPT_ARCHIVE_ALLOW_REMOTE_UPLOADS=true
set CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES=10737418240
python chatgpt_archive.py web --host 0.0.0.0 --port 8787
```

Para usar el profile completo de subida local en una red interna de confianza:

```bash
# macOS / Linux
export CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE=local
python chatgpt_archive.py web --host 0.0.0.0 --port 8787
```

## Sintaxis de búsqueda

La búsqueda admite términos simples, frases entre comillas, `OR` en mayúsculas, exclusiones como `-term` y filtros como `role:user`, `source:zip`, `path:current`, `path:all`, `scope:title` y `scope:message`. Las consultas son texto de búsqueda, no SQL directo.

Los filtros y rangos de fecha usan días UTC. Cmd/Ctrl+F solo ve las filas renderizadas; para contenido no cargado usa la búsqueda del archivo o copia la conversación.

```bash
python chatgpt_archive.py search --db archive/chatgpt_archive.db "python sqlite"
python chatgpt_archive.py search --db archive/chatgpt_archive.db "\"exact phrase\""
python chatgpt_archive.py search --db archive/chatgpt_archive.db "role:user path:current python -pandas"
```

La búsqueda Web usa índices opcionales normalized trigram creados por `web-index`. Está pensada para búsquedas prácticas por subcadena en el navegador. Si esos índices opcionales faltan o se dañan, reconstruye:

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
```

## Verificación e índices Web opcionales

Ejecuta `verify` después de importar para comprobar SQLite y los enlaces de las conversaciones.

```bash
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
```

Los índices FTS de CLI y de subcadenas Web son opcionales. Si `verify` informa de un error en un índice Web opcional, reconstrúyelo y vuelve a verificar:

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
```

## Logging

Los niveles de log son `debug`, `info`, `warning`, `error` y `none`; el predeterminado es `warning`. Los logs contienen estados y recuentos, no títulos, mensajes, fragmentos ni JSON sin procesar.

Las opciones de logging pueden ir antes o después del subcomando:

```bash
python chatgpt_archive.py --log-level debug web
python chatgpt_archive.py web --log-level debug
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --log-level info --log-file logs/import.log
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --json-logs --log-file logs/import.jsonl
```

## Construir desde las fuentes

Estos comandos ejecutan las comprobaciones del proyecto, construyen la Web UI, eliminan archivos generados y comprueban una entrega ejecutable.

```bash
python -m compileall chatgpt_archive.py chatgpt_export_archiver tests tools
python -m unittest discover -s tests -v
python tools/clean_generated_artifacts.py --fail-on-blocked
python tools/check_delivery_clean.py --mode runnable .
```

Construye la Web UI y ejecuta smoke tests:

```bash
cd webui
npm ci
npm run typecheck
npm run build
npm run test:python-resolution
npm run test:dom
cd ..
python tools/clean_generated_artifacts.py --fail-on-blocked
python tools/check_delivery_clean.py --mode runnable .
```

Para comprobar un ZIP de entrega:

```bash
python tools/check_delivery_clean.py --mode runnable path/to/delivery.zip
```

## Contenido del paquete

Un paquete ejecutable incluye el código Python, las pruebas, la documentación, las dependencias y el código Web y los recursos construidos de `webui/dist`. Mantén fuera las bases de datos, exportaciones, ZIP, logs, cachés, `node_modules` y otros datos locales.

## Resumen de la base de datos

SQLite guarda conversaciones, mensajes, historial de importaciones, avisos e índices de búsqueda opcionales. Si aparece `database_migration_required`, haz una copia de seguridad y ejecuta `python chatgpt_archive.py migrate --db archive/chatgpt_archive.db`.

## Límites conocidos

- Es una herramienta local de archivo, no un servicio de sincronización en la nube.
- La Web UI está pensada para uso local. No la expongas a redes no confiables sin añadir tus propios controles de acceso.
- El parser sigue el formato de exportación de OpenAI / ChatGPT observado hasta ahora. Si cambia el formato de origen, actualiza `inspect` y las pruebas antes de confiar en una nueva ruta de importación.
- Las partes de nombres de archivo exportados se sanean para Windows y sistemas tipo Unix, incluidos nombres reservados de dispositivo como `CON`, `AUX`, `COM1`, `LPT9`, `COM¹` y `LPT²`, además de puntos y espacios finales.
- Los archivos muy grandes pueden tardar en importarse, reconstruir FTS y crear índices Web trigram. Para importaciones grandes, prefiere la ruta `--rebuild-fts`.

## Notas de seguridad

Las importaciones, subidas, búsquedas, copias, exportaciones y escrituras usan entradas acotadas y errores seguros sin contenido. Mantén la Web UI en loopback salvo que confíes en la red, conserva los ZIP originales hasta completar la verificación y deja espacio suficiente para la base, archivos temporales, WAL e índices.
