# ChatGPT Export Archiver

Language: [English](README.md) | [简体中文](README.zh-CN.md) | [繁體中文（臺灣）](README.zh-TW.md) | [日本語](README.ja-JP.md) | [Español](README.es-ES.md)

Turn an official ChatGPT export ZIP into a private, searchable SQLite archive. Import from the CLI or local Web UI, read and search conversations in your browser, and export them as Markdown or TXT. Everything runs on your computer.

`Local-first` · `SQLite` · `Private search` · `CLI` · `Web UI` · `Markdown/TXT export`

## Why Use It

- **Private and local.** ZIP files, databases, exports, temporary uploads, the Web UI, and logs stay on your computer.
- **Direct and incremental import.** Read official ZIP files or extracted JSON shards without merging them, and update an existing archive safely.
- **Comfortable reading and search.** Use the chat-style browser, classic layout, filters, phrases, exclusions, pagination, and optional fast indexes.
- **Portable output.** Export deterministic Markdown or TXT for backup, offline use, or migration.

## Screenshot

Screenshots must use synthetic conversations and must not show real titles, messages, file names, email addresses, or local paths.

## What This Project Does

- Imports `conversations.json` and sharded `conversations-*.json` files from an OpenAI / ChatGPT export ZIP, a standalone `conversations.json` file, or an extracted export directory into SQLite.
- Preserves conversation metadata, mapping nodes, message roles, text content, timestamps, parent links, source tracking, and import warnings.
- Supports incremental imports. Re-importing a newer export updates changed conversations without intentionally duplicating unchanged data.
- Builds an optional FTS5 message index for CLI search.
- Builds optional Web substring indexes for faster browser search.
- Exports conversations as Markdown, TXT, or both.
- Provides `verify`, `stats`, and privacy-preserving `inspect` commands that avoid printing message text.
- Provides a local Web UI that can start without an existing database and can import ZIP files from the browser.
- Keeps logging separate from structured command output and avoids logging titles, snippets, raw JSON, or message bodies.

## Privacy

Everything runs locally and the Web UI binds to `127.0.0.1` by default. Normal imports never remove the original ZIP; browser uploads use a temporary server-side copy. Databases and exported files contain private conversation data, so store them securely.

## Requirements

- Python 3.10 or newer; Web dependencies are tested with Python 3.13.
- SQLite with JSON support. FTS5 is optional because scan search remains available.
- Node.js and npm are needed only to rebuild or test the React Web UI.
- CLI commands use the Python standard library. Install `requirements-web.txt` to run the Web UI and browser ZIP upload.

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements-web.txt -c constraints-web-py313.txt
```

On Windows PowerShell:

```bash
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -r requirements-web.txt -c constraints-web-py313.txt
```

On Windows cmd.exe:

```bash
py -3 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install -U pip
python -m pip install -r requirements-web.txt -c constraints-web-py313.txt
```

## Quick start

Put your ChatGPT export ZIP somewhere outside the repository, then run the fastest safe import command. This skips input hashing and rebuilds FTS once at the end, which is much faster for large archives than maintaining FTS row by row.

```bash
NEW_ZIP="$HOME/Downloads/chatgpt_export/chatgpt_export.zip"
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

Windows PowerShell equivalent:

```bash
$env:NEW_ZIP = "$env:USERPROFILE\Downloads\chatgpt-export.zip"
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$env:NEW_ZIP" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

Windows cmd.exe equivalent:

```bash
set NEW_ZIP=%USERPROFILE%\Downloads\chatgpt-export.zip
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "%NEW_ZIP%" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

Start the local Web UI:

```bash
python chatgpt_archive.py web --db archive/chatgpt_archive.db --port 8787
```

If no database exists yet, the Web UI still starts and shows an empty state with an import panel. You can choose a ChatGPT export ZIP in the browser; the backend writes a temporary local copy, imports it, then automatically runs `verify`, `stats`, and `web-index`.

```bash
python chatgpt_archive.py web --port 8787
```

## Quick CLI workflow

Inspect an export without printing chat content:

```bash
python chatgpt_archive.py inspect --input "$NEW_ZIP"
```

Create an empty database explicitly:

```bash
python chatgpt_archive.py init --db archive/chatgpt_archive.db
```

Import an export with the large-archive path:

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
```

`--input` may point at the official export ZIP, a standalone `conversations.json`, or an extracted export directory. Extracted directories may contain either `conversations.json` or sharded `conversations-*.json` files; do not merge shards manually.

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input conversations.json --no-input-sha256 --rebuild-fts
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input ./extracted-export/ --no-input-sha256 --rebuild-fts
```

Verify structural consistency:

```bash
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
```

Show structured counts and time bounds:

```bash
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

Search message text through the CLI search path. This prints conversation IDs, node IDs, and roles, not snippets:

```bash
python chatgpt_archive.py search --db archive/chatgpt_archive.db --limit 20 "python sqlite"
```

Export the conversation as Markdown, TXT, or both formats in the same run. `--format md` writes Markdown body files and updates the manifest, `--format txt` writes plain text body files and updates the manifest, and `--format all` writes both body formats and updates the manifest:

```bash
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format md --out exports
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format txt --out exports
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format all --out exports
```

Export a date range and rewrite existing files if needed. Date boundaries for `--from` and `--to` accept only `YYYY-MM-DD`:

```bash
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format md --out exports --from 2024-01-01 --to 2024-12-31 --force
```

Rebuild optional Web search indexes:

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
```

## Import modes

The recommended large-archive command is:

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
```

If you want SQLite to spend extra time tidying planner statistics and the FTS index after import, use:

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts --optimize-after-import --optimize-fts-after-import
```

`--delete-input-on-success` is retained for compatibility but is unavailable and fails before import. Normal imports never delete the original ZIP; remove it yourself only after verifying the database.

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts --delete-input-on-success
```

## Web UI workflow

Use the Web startup command in the setup section. The page can open an existing database or start empty and import a ZIP. It provides paginated chat and classic layouts, search, internal-message controls, copy, and download. Very long matches recovered from older export data can be opened and copied in full; if that temporary search session expires, run the search again. Browser Back and Forward restore submitted searches, filters, paths, selected conversations, and layout without adding an entry for every keystroke.

## Web upload security limits

Browser uploads are checked before import. Loopback access uses the large local defaults below. Non-loopback access must be enabled explicitly and uses smaller safe defaults unless you deliberately choose the trusted local profile. CLI `import` is not affected by these upload limits.

| Environment variable | Local default | Controls |
|---|---|---|
| `CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES` | 20 GiB | Total compressed ZIP upload size |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_JSON_MEMBER_BYTES` | 64 GiB | Max uncompressed size of a single JSON member |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_JSON_MEMBERS` | 5,000 | Max number of conversation JSON members |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_TOTAL_UNCOMPRESSED_BYTES` | 128 GiB | Max total uncompressed JSON data |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_COMPRESSION_RATIO` | 1,000.0 | Max compression ratio for large JSON members |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_TOTAL_MEMBERS` | 100,000 | Max total ZIP members |
| `CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE` | unset | Set to `local` only on trusted non-loopback networks to use local defaults for unset upload limits |

To raise a local limit for a legitimate large archive, set the corresponding variable before starting the Web UI:

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

To allow an explicit large compressed ZIP limit on a trusted internal network while keeping other unset limits remote-safe:

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

To use the full local upload profile on a trusted internal network:

```bash
# macOS / Linux
export CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE=local
python chatgpt_archive.py web --host 0.0.0.0 --port 8787
```

## Search syntax

Search accepts plain terms, quoted phrases, uppercase `OR`, exclusions such as `-term`, and filters such as `role:user`, `source:zip`, `path:current`, `path:all`, `scope:title`, and `scope:message`. Queries are search text, not raw SQL.

Date filters and export date ranges use UTC calendar days. Browser Cmd/Ctrl+F sees only rendered rows, so use archive search or conversation copy for content outside the loaded page.

```bash
python chatgpt_archive.py search --db archive/chatgpt_archive.db "python sqlite"
python chatgpt_archive.py search --db archive/chatgpt_archive.db "\"exact phrase\""
python chatgpt_archive.py search --db archive/chatgpt_archive.db "role:user path:current python -pandas"
```

Web search uses optional normalized trigram indexes built by `web-index`. This is designed for practical substring search in the browser. If those optional indexes are missing or damaged, rebuild them:

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
```

## Verification and optional Web indexes

Run `verify` after import to check SQLite and conversation links.

```bash
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
```

CLI FTS and Web substring indexes are optional. If `verify` reports an optional Web index error, rebuild the index and verify again:

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
```

## Logging

Log levels are `debug`, `info`, `warning`, `error`, and `none`; the default is `warning`. Logs contain status and counts, not conversation titles, messages, snippets, or raw JSON.

Logging flags can be placed before or after the subcommand:

```bash
python chatgpt_archive.py --log-level debug web
python chatgpt_archive.py web --log-level debug
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --log-level info --log-file logs/import.log
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --json-logs --log-file logs/import.jsonl
```

## Build from source

These commands run the project checks, build the Web UI, remove generated files, and verify a runnable delivery.

```bash
python -m compileall chatgpt_archive.py chatgpt_export_archiver tests tools
python -m unittest discover -s tests -v
python tools/clean_generated_artifacts.py --fail-on-blocked
python tools/check_delivery_clean.py --mode runnable .
```

Build and smoke-test the Web UI:

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

When checking a ZIP delivery:

```bash
python tools/check_delivery_clean.py --mode runnable path/to/delivery.zip
```

## Package contents

A runnable package includes Python source, tests, documentation, Web dependencies and source, and built `webui/dist` assets. Keep databases, exports, ZIP files, logs, caches, `node_modules`, and other local data outside the package.

## Database overview

SQLite stores conversations, messages, import history, warnings, and optional search indexes. If a database reports `database_migration_required`, back it up and run `python chatgpt_archive.py migrate --db archive/chatgpt_archive.db`.

## Known limits

- This is a local archive tool, not a cloud sync service.
- The Web UI is intended for local use. Do not expose it to an untrusted network without adding your own access controls.
- Export parsing follows the observed OpenAI / ChatGPT export format. If the upstream export shape changes, `inspect` and tests should be updated before trusting a new import path.
- Export file name parts are sanitized for Windows as well as Unix-like systems, including reserved device names such as `CON`, `AUX`, `COM1`, `LPT9`, `COM¹`, and `LPT²`, plus trailing dots and spaces.
- Very large archives can take time to import, rebuild FTS, and build Web trigram indexes. Prefer the `--rebuild-fts` path for large imports.

## Safety notes

Imports, uploads, search, copy, export, and database writes use bounded inputs and content-safe errors. Keep the Web UI on loopback unless you trust the network, retain original ZIP files until verification succeeds, and leave enough disk space for the database, temporary files, WAL, and indexes.
