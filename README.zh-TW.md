# ChatGPT Export Archiver

語言: [English](README.md) | [简体中文](README.zh-CN.md) | [繁體中文（臺灣）](README.zh-TW.md) | [日本語](README.ja-JP.md) | [Español](README.es-ES.md)

將官方 ChatGPT 匯出 ZIP 轉成私有、可搜尋的 SQLite 封存。可透過 CLI 或本機 Web UI 匯入，在瀏覽器中閱讀和搜尋對話，並匯出為 Markdown 或 TXT。所有功能都在你的電腦上執行。

`本機優先` · `SQLite` · `私有搜尋` · `CLI` · `Web UI` · `Markdown/TXT 匯出`

## 為什麼值得用

- **私有且在本機。** ZIP、資料庫、匯出檔案、暫存上傳、Web UI 和日誌都保存在你的電腦上。
- **直接增量匯入。** 無需合併即可讀取官方 ZIP 或已解壓縮的 JSON 分片，並安全更新現有封存。
- **便於閱讀和搜尋。** 使用聊天式閱讀器、經典版面、篩選器、片語、排除條件、分頁和可選快速索引。
- **便於移轉。** 匯出確定性的 Markdown 或 TXT，用於備份、離線使用或移轉。

## 截圖

截圖只能使用合成對話，不得顯示真實標題、訊息、檔名、電子郵件地址或本機路徑。

## 專案功能

- 從 OpenAI / ChatGPT 匯出的 ZIP、單一 `conversations.json` 檔案或解壓縮後的匯出目錄匯入 `conversations.json` 和 sharded `conversations-*.json`。
- 保留對話中繼資料、mapping nodes、訊息角色、文字內容、時間戳記、父節點關係、來源追蹤與匯入警告。
- 支援增量匯入。把較新的匯出檔再次匯入同一個資料庫時，會更新已變更的對話，不會刻意重複寫入未變更的資料。
- 建立可選的 FTS5 訊息索引，供命令列搜尋使用。
- 建立可選的 Web 子字串搜尋索引，提升瀏覽器搜尋體驗。
- 支援匯出 Markdown、TXT，或同時匯出兩種格式。
- 提供 `verify`、`stats` 與不會列印聊天正文的隱私友善 `inspect` 指令。
- 提供本機 Web UI。即使資料庫尚未存在，也可以先啟動頁面，再從瀏覽器選取 ZIP 匯入。
- 日誌與結構化指令輸出分離，並避免記錄標題、snippet、raw JSON 或訊息正文。

## 隱私

所有功能都在本機執行，Web UI 預設繫結 `127.0.0.1`。一般匯入不會刪除原始 ZIP；瀏覽器上傳使用伺服器端暫存副本。資料庫和匯出檔案包含私密對話內容，請妥善保存。

## 系統需求

- Python 3.10 或更高版本；Web 相依套件使用 Python 3.13 測試。
- SQLite 需要 JSON 支援。FTS5 可選，因為仍可使用掃描搜尋。
- 只有重建或測試 React Web UI 時才需要 Node.js 和 npm。
- CLI 使用 Python 標準函式庫。執行 Web UI 和瀏覽器 ZIP 上傳時安裝 `requirements-web.txt`。

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

## 快速開始

把 ChatGPT 匯出 ZIP 放在儲存庫外部，然後執行最快的安全匯入指令。這個指令會略過輸入雜湊，並在匯入結尾一次重建 FTS；對大型封存來說，比逐筆維護 FTS 快得多。

```bash
NEW_ZIP="$HOME/Downloads/chatgpt_export/chatgpt_export.zip"
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

Windows PowerShell 等價寫法：

```bash
$env:NEW_ZIP = "$env:USERPROFILE\Downloads\chatgpt-export.zip"
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$env:NEW_ZIP" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

Windows cmd.exe 等價寫法：

```bash
set NEW_ZIP=%USERPROFILE%\Downloads\chatgpt-export.zip
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "%NEW_ZIP%" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

啟動本機 Web UI：

```bash
python chatgpt_archive.py web --db archive/chatgpt_archive.db --port 8787
```

如果還沒有資料庫，Web UI 仍可啟動，並會顯示空狀態與匯入面板。你可以在瀏覽器中選取 ChatGPT 匯出 ZIP；後端會寫入一個本機暫存副本，完成匯入後自動執行 `verify`、`stats` 與 `web-index`。

```bash
python chatgpt_archive.py web --port 8787
```

## 常用 CLI 流程

檢查匯出檔，但不列印聊天內容：

```bash
python chatgpt_archive.py inspect --input "$NEW_ZIP"
```

明確建立空資料庫：

```bash
python chatgpt_archive.py init --db archive/chatgpt_archive.db
```

使用大型封存路徑匯入：

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
```

`--input` 可以指向官方匯出 ZIP、單一 `conversations.json`，或解壓縮後的匯出目錄。解壓縮目錄可以包含 `conversations.json`，也可以包含分片形式的 `conversations-*.json`；不要手動合併分片。

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input conversations.json --no-input-sha256 --rebuild-fts
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input ./extracted-export/ --no-input-sha256 --rebuild-fts
```

檢查結構一致性：

```bash
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
```

查看結構化計數與時間範圍：

```bash
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

透過 CLI 搜尋路徑搜尋訊息文字。輸出只包含 conversation ID、node ID 與角色，不包含 snippet：

```bash
python chatgpt_archive.py search --db archive/chatgpt_archive.db --limit 20 "python sqlite"
```

將對話匯出為 Markdown、TXT，或在同一次執行中同時匯出兩種格式。`--format md` 會寫入 Markdown 正文檔並更新 manifest，`--format txt` 會寫入 plain text 正文檔並更新 manifest，`--format all` 會同時寫入兩種正文檔並更新 manifest：

```bash
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format md --out exports
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format txt --out exports
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format all --out exports
```

依日期範圍匯出，並在需要時重寫既有檔案。`--from` 與 `--to` 的日期邊界只接受 `YYYY-MM-DD`：

```bash
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format md --out exports --from 2024-01-01 --to 2024-12-31 --force
```

重建可選 Web 搜尋索引：

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
```

## 匯入模式

建議的大型封存指令是：

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
```

如果願意讓 SQLite 在匯入後額外花時間整理 planner statistics 與 FTS 索引，可以使用：

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts --optimize-after-import --optimize-fts-after-import
```

`--delete-input-on-success` 僅為相容性保留，但不可用，並會在匯入前失敗。一般匯入不會刪除原始 ZIP；請在驗證資料庫後自行刪除。

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts --delete-input-on-success
```

## Web UI 流程

使用安裝部分的 Web 啟動命令。頁面可開啟現有資料庫，也可在沒有資料庫時啟動並匯入 ZIP，並提供分頁的聊天與經典版面、搜尋、內部訊息控制、複製和下載。從舊匯出資料找回的超長符合內容也可完整開啟和複製；如果暫時搜尋工作階段已過期，重新搜尋即可。瀏覽器上一頁和下一頁會還原已提交的搜尋、篩選、路徑、對話選擇和版面，輸入文字時不會為每個按鍵增加歷程記錄。

## Web 上傳安全限制

瀏覽器上傳會在匯入前檢查檔案。回環存取使用下表中的本機大檔案預設值；非回環存取必須明確啟用，並使用較小的安全預設值，除非主動選擇可信本機設定。CLI `import` 不受這些上傳限制影響。

| 環境變數 | 本機預設值 | 控制內容 |
|---|---|---|
| `CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES` | 20 GiB | 壓縮 ZIP 上傳總大小 |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_JSON_MEMBER_BYTES` | 64 GiB | 單個 JSON member 最大未壓縮大小 |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_JSON_MEMBERS` | 5,000 | conversation JSON member 最大數量 |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_TOTAL_UNCOMPRESSED_BYTES` | 128 GiB | 未壓縮 JSON 資料總量上限 |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_COMPRESSION_RATIO` | 1,000.0 | 大型 JSON member 最大壓縮比 |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_TOTAL_MEMBERS` | 100,000 | ZIP 內總 member 數上限 |
| `CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE` | 未設定 | 只在可信非 loopback 網路上設為 `local`，讓未設定的上傳限制使用本機預設值 |

如需為合法的超大型封存提高本機限制，在啟動 Web UI 前設定相應變數：

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

在可信內部網路只提高明確的壓縮 ZIP 上限，同時讓其他未設定限制保持 remote-safe：

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

在可信內部網路使用完整本機上傳 profile：

```bash
# macOS / Linux
export CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE=local
python chatgpt_archive.py web --host 0.0.0.0 --port 8787
```

## 搜尋語法

搜尋支援一般詞語、引號片語、大寫 `OR`、`-term` 形式的排除條件，以及 `role:user`、`source:zip`、`path:current`、`path:all`、`scope:title` 和 `scope:message` 等篩選器。查詢是搜尋文字，不是原始 SQL。

日期篩選和匯出日期範圍使用 UTC 日曆日。瀏覽器 Cmd/Ctrl+F 只能看到已算繪的內容；查找未載入內容時請使用封存搜尋或複製對話。

```bash
python chatgpt_archive.py search --db archive/chatgpt_archive.db "python sqlite"
python chatgpt_archive.py search --db archive/chatgpt_archive.db "\"exact phrase\""
python chatgpt_archive.py search --db archive/chatgpt_archive.db "role:user path:current python -pandas"
```

Web 搜尋使用 `web-index` 建立的可選 normalized trigram 索引，適合瀏覽器中的實用子字串搜尋。如果這些可選索引缺失或損壞，請重建：

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
```

## 驗證與可選 Web 索引

匯入後執行 `verify` 檢查 SQLite 和對話連結。

```bash
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
```

CLI FTS 和 Web 子字串索引都是可選的。如果 `verify` 回報可選 Web 索引錯誤，請重建索引後再次驗證：

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
```

## 日誌

日誌層級為 `debug`、`info`、`warning`、`error` 和 `none`，預設為 `warning`。日誌只包含狀態和計數，不包含對話標題、訊息、片段或原始 JSON。

日誌參數可以寫在子命令之前，也可以寫在子命令之後：

```bash
python chatgpt_archive.py --log-level debug web
python chatgpt_archive.py web --log-level debug
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --log-level info --log-file logs/import.log
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --json-logs --log-file logs/import.jsonl
```

## 從原始碼建置

以下命令用於執行專案檢查、建置 Web UI、清理產生的檔案並檢查可執行交付套件。

```bash
python -m compileall chatgpt_archive.py chatgpt_export_archiver tests tools
python -m unittest discover -s tests -v
python tools/clean_generated_artifacts.py --fail-on-blocked
python tools/check_delivery_clean.py --mode runnable .
```

建置並 smoke-test Web UI：

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

檢查 ZIP 交付包：

```bash
python tools/check_delivery_clean.py --mode runnable path/to/delivery.zip
```

## 套件內容

可執行套件包含 Python 原始碼、測試、文件、Web 相依套件與原始碼，以及建置完成的 `webui/dist`。資料庫、匯出檔案、ZIP、日誌、快取、`node_modules` 和其他本機資料不應放入套件。

## 資料庫概覽

SQLite 保存對話、訊息、匯入記錄、警告和可選搜尋索引。資料庫出現 `database_migration_required` 時，請先備份，再執行 `python chatgpt_archive.py migrate --db archive/chatgpt_archive.db`。

## 已知限制

- 這是本機封存工具，不是雲端同步服務。
- Web UI 供本機使用。不要在沒有額外存取控制的情況下暴露到不可信網路。
- 匯出解析遵循目前觀察到的 OpenAI / ChatGPT 匯出格式。如果上游匯出結構變更，應先更新 `inspect` 與測試，再信任新的匯入路徑。
- 匯出檔名片段會同時依 Windows 和類 Unix 系統清理，包括 `CON`、`AUX`、`COM1`、`LPT9`、`COM¹`、`LPT²` 等保留裝置名稱，以及尾隨點和空格。
- 超大型封存在匯入、重建 FTS 與建立 Web trigram 索引時都可能需要時間。大型匯入優先使用 `--rebuild-fts` 路徑。

## 安全提示

匯入、上傳、搜尋、複製、匯出和資料庫寫入都使用有界輸入與不含內容的安全錯誤。除非信任所在網路，否則只在回環位址執行 Web UI；驗證成功前保留原始 ZIP，並為資料庫、暫存檔案、WAL 和索引預留足夠磁碟空間。
