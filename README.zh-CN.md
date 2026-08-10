# ChatGPT Export Archiver

语言: [English](README.md) | [简体中文](README.zh-CN.md) | [繁體中文（臺灣）](README.zh-TW.md) | [日本語](README.ja-JP.md) | [Español](README.es-ES.md)

把官方 ChatGPT 导出 ZIP 变成私有、可搜索的 SQLite 归档。可通过 CLI 或本地 Web UI 导入，在浏览器中阅读和搜索对话，并导出为 Markdown 或 TXT。所有功能都在你的电脑上运行。

`本地优先` · `SQLite` · `私有搜索` · `CLI` · `Web UI` · `Markdown/TXT 导出`

## 为什么值得用

- **私有且本地。** ZIP、数据库、导出文件、临时上传、Web UI 和日志都保存在你的电脑上。
- **直接增量导入。** 无需合并即可读取官方 ZIP 或已解压的 JSON 分片，并安全更新已有归档。
- **便于阅读和搜索。** 使用聊天式阅读器、经典布局、筛选器、短语、排除条件、分页和可选快速索引。
- **便于迁移。** 导出确定性的 Markdown 或 TXT，用于备份、离线使用或迁移。

## 截图

截图只能使用合成对话，不得显示真实标题、消息、文件名、电子邮件地址或本地路径。

## 项目功能

- 从 OpenAI / ChatGPT 导出的 ZIP、单个 `conversations.json` 文件或解压后的导出目录中导入 `conversations.json` 和 sharded `conversations-*.json`。
- 保存会话元数据、mapping nodes、消息角色、正文文本、时间戳、父节点关系、来源跟踪和导入警告。
- 支持增量导入。把较新的导出文件再次导入同一个数据库时，会更新发生变化的会话，而不是有意重复写入未变化的数据。
- 建立可选的 FTS5 消息索引，供命令行搜索使用。
- 建立可选的 Web 子串搜索索引，提升浏览器搜索体验。
- 支持导出 Markdown、TXT 或两种格式同时导出。
- 提供 `verify`、`stats` 和不会打印聊天正文的隐私友好型 `inspect` 命令。
- 提供本地 Web UI。即使数据库还不存在，也可以先启动页面，再从浏览器选择 ZIP 导入。
- 日志与结构化命令输出分离，并避免记录标题、snippet、raw JSON 或消息正文。

## 隐私

所有功能都在本地运行，Web UI 默认绑定 `127.0.0.1`。普通导入不会删除原始 ZIP；浏览器上传使用服务器端临时副本。数据库和导出文件包含私密对话内容，请妥善保存。

## 环境要求

- Python 3.10 或更高版本；Web 依赖使用 Python 3.13 测试。
- SQLite 需要 JSON 支持。FTS5 可选，因为仍可使用扫描搜索。
- 只有重建或测试 React Web UI 时才需要 Node.js 和 npm。
- CLI 使用 Python 标准库。运行 Web UI 和浏览器 ZIP 上传时安装 `requirements-web.txt`。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements-web.txt -c constraints-web-py313.txt
```

Windows PowerShell：

```bash
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -r requirements-web.txt -c constraints-web-py313.txt
```

Windows cmd.exe：

```bash
py -3 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install -U pip
python -m pip install -r requirements-web.txt -c constraints-web-py313.txt
```

## 快速开始

把 ChatGPT 导出 ZIP 放在仓库外部，然后运行最快的安全导入命令。这个命令跳过输入哈希，并在导入末尾一次性重建 FTS；对大型归档来说，比逐条维护 FTS 快得多。

```bash
NEW_ZIP="$HOME/Downloads/chatgpt_export/chatgpt_export.zip"
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

Windows PowerShell 等价写法：

```bash
$env:NEW_ZIP = "$env:USERPROFILE\Downloads\chatgpt-export.zip"
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$env:NEW_ZIP" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

Windows cmd.exe 等价写法：

```bash
set NEW_ZIP=%USERPROFILE%\Downloads\chatgpt-export.zip
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "%NEW_ZIP%" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

启动本地 Web UI：

```bash
python chatgpt_archive.py web --db archive/chatgpt_archive.db --port 8787
```

如果还没有数据库，Web UI 也能启动，并会显示空状态和导入面板。你可以在浏览器中选择 ChatGPT 导出 ZIP；后端会写入一个本地临时副本，完成导入后自动运行 `verify`、`stats` 和 `web-index`。

```bash
python chatgpt_archive.py web --port 8787
```

## 常用 CLI 流程

检查导出文件，但不打印聊天内容：

```bash
python chatgpt_archive.py inspect --input "$NEW_ZIP"
```

显式建立一个空数据库：

```bash
python chatgpt_archive.py init --db archive/chatgpt_archive.db
```

使用大型归档路径导入：

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
```

`--input` 可以指向官方导出 ZIP、单个 `conversations.json`，或解压后的导出目录。解压目录可以包含 `conversations.json`，也可以包含分片形式的 `conversations-*.json`；不要手动合并分片。

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input conversations.json --no-input-sha256 --rebuild-fts
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input ./extracted-export/ --no-input-sha256 --rebuild-fts
```

检查结构一致性：

```bash
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
```

查看结构化计数和时间边界：

```bash
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

通过 CLI 搜索路径搜索消息正文。输出只包含 conversation ID、node ID 和角色，不包含 snippet：

```bash
python chatgpt_archive.py search --db archive/chatgpt_archive.db --limit 20 "python sqlite"
```

把会话导出为 Markdown、TXT，或在同一次运行中同时导出两种格式。`--format md` 写 Markdown 正文文件并更新 manifest，`--format txt` 写 plain text 正文文件并更新 manifest，`--format all` 同时写两种正文文件并更新 manifest：

```bash
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format md --out exports
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format txt --out exports
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format all --out exports
```

按日期范围导出，并在必要时重写已有文件。`--from` 和 `--to` 的日期边界只接受 `YYYY-MM-DD`：

```bash
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format md --out exports --from 2024-01-01 --to 2024-12-31 --force
```

重建可选 Web 搜索索引：

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
```

启动 Web UI：

```bash
python chatgpt_archive.py web --db archive/chatgpt_archive.db --port 8787
```

## 导入模式

推荐的大型归档命令是：

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
```

如果你愿意让 SQLite 在导入后额外花时间整理 planner statistics 和 FTS 索引，可以使用：

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts --optimize-after-import --optimize-fts-after-import
```

`--delete-input-on-success` 仅为兼容保留，但不可用，并会在导入前失败。普通导入不会删除原始 ZIP；请在验证数据库后自行删除。

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts --delete-input-on-success
```

## Web UI 流程

使用下面的命令启动本地 Web UI。它可打开已有数据库，也可在没有数据库时启动并从浏览器导入 ZIP。阅读器提供分页的聊天与经典布局、搜索、内部消息控制、复制和下载。

```bash
python chatgpt_archive.py web --port 8787
```

## Web 上传安全限制

浏览器上传会在导入前检查文件。回环访问使用下表中的本地大文件默认值；非回环访问必须显式启用，并使用较小的安全默认值，除非主动选择可信本地配置。CLI `import` 不受这些上传限制影响。

| 环境变量 | 本机默认值 | 控制内容 |
|---|---|---|
| `CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES` | 20 GiB | 压缩 ZIP 上传总大小 |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_JSON_MEMBER_BYTES` | 64 GiB | 单个 JSON member 最大未压缩大小 |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_JSON_MEMBERS` | 5,000 | conversation JSON member 最大数量 |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_TOTAL_UNCOMPRESSED_BYTES` | 128 GiB | 未压缩 JSON 数据总量上限 |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_COMPRESSION_RATIO` | 1,000.0 | 大型 JSON member 最大压缩比 |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_TOTAL_MEMBERS` | 100,000 | ZIP 内总 member 数上限 |
| `CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE` | 未设置 | 只在可信非 loopback 网络上设为 `local`，让未设置的上传限制使用本机默认值 |

如需为合法的大型归档提高本机限制，在启动 Web UI 前设置相应变量：

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

在可信内网只提高显式压缩 ZIP 上限，同时让其他未设置限制保持 remote-safe：

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

在可信内网使用完整本机上传 profile：

```bash
# macOS / Linux
export CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE=local
python chatgpt_archive.py web --host 0.0.0.0 --port 8787
```

## 搜索语法

搜索支持普通词、引号短语、大写 `OR`、`-term` 形式的排除条件，以及 `role:user`、`source:zip`、`path:current`、`path:all`、`scope:title` 和 `scope:message` 等筛选器。查询是搜索文本，不是原始 SQL。

日期筛选和导出日期范围使用 UTC 日历日。浏览器 Cmd/Ctrl+F 只能看到已渲染的内容；查找未加载内容时请使用归档搜索或复制对话。

```bash
python chatgpt_archive.py search --db archive/chatgpt_archive.db "python sqlite"
python chatgpt_archive.py search --db archive/chatgpt_archive.db "\"exact phrase\""
python chatgpt_archive.py search --db archive/chatgpt_archive.db "role:user path:current python -pandas"
```

Web 搜索使用 `web-index` 建立的可选 normalized trigram 索引，适合浏览器中的实用子串搜索。如果这些可选索引缺失或损坏，请重建：

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
```

## 验证与可选 Web 索引

导入后运行 `verify` 检查 SQLite 和对话链接。

```bash
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
```

CLI FTS 和 Web 子串索引都是可选的。如果 `verify` 报告可选 Web 索引错误，请重建索引后再次验证：

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
```

## 日志

日志级别为 `debug`、`info`、`warning`、`error` 和 `none`，默认为 `warning`。日志只包含状态和计数，不包含对话标题、消息、片段或原始 JSON。

日志参数可以写在子命令之前，也可以写在子命令之后：

```bash
python chatgpt_archive.py --log-level debug web
python chatgpt_archive.py web --log-level debug
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --log-level info --log-file logs/import.log
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --json-logs --log-file logs/import.jsonl
```

## 从源码构建

以下命令用于运行项目检查、构建 Web UI、清理生成文件并检查可运行交付包。

```bash
python -m compileall chatgpt_archive.py chatgpt_export_archiver tests tools
python -m unittest discover -s tests -v
python tools/clean_generated_artifacts.py --fail-on-blocked
python tools/check_delivery_clean.py --mode runnable .
```

构建并 smoke-test Web UI：

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

检查 ZIP 交付包：

```bash
python tools/check_delivery_clean.py --mode runnable path/to/delivery.zip
```

## 软件包内容

可运行软件包包含 Python 源码、测试、文档、Web 依赖与源码，以及构建好的 `webui/dist`。数据库、导出文件、ZIP、日志、缓存、`node_modules` 和其他本地数据不应放入软件包。

## 数据库概览

SQLite 保存对话、消息、导入记录、警告和可选搜索索引。数据库出现 `database_migration_required` 时，请先备份，再运行 `python chatgpt_archive.py migrate --db archive/chatgpt_archive.db`。

## 已知限制

- 这是本地归档工具，不是云同步服务。
- Web UI 面向本地使用。不要在没有额外访问控制的情况下暴露到不可信网络。
- 导出解析遵循目前观察到的 OpenAI / ChatGPT 导出格式。如果上游导出结构变化，应先更新 `inspect` 和测试，再信任新的导入路径。
- 导出文件名片段会同时按 Windows 和类 Unix 系统清理，包括 `CON`、`AUX`、`COM1`、`LPT9`、`COM¹`、`LPT²` 等保留设备名，以及尾随点和空格。
- 超大型归档在导入、重建 FTS 和构建 Web trigram 索引时都可能需要时间。大型导入优先使用 `--rebuild-fts` 路径。

## 安全提示

导入、上传、搜索、复制、导出和数据库写入都使用有界输入与不含内容的安全错误。除非信任所在网络，否则只在回环地址运行 Web UI；验证成功前保留原始 ZIP，并为数据库、临时文件、WAL 和索引预留足够磁盘空间。
