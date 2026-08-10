# ChatGPT Export Archiver

言語: [English](README.md) | [简体中文](README.zh-CN.md) | [繁體中文（臺灣）](README.zh-TW.md) | [日本語](README.ja-JP.md) | [Español](README.es-ES.md)

公式 ChatGPT エクスポート ZIP を、非公開で検索可能な SQLite アーカイブに変換します。CLI またはローカル Web UI からインポートし、ブラウザーで会話を読んだり検索したり、Markdown/TXT へエクスポートしたりできます。すべて自分のコンピューター上で動作します。

`ローカル優先` · `SQLite` · `非公開検索` · `CLI` · `Web UI` · `Markdown/TXT エクスポート`

## 使う理由

- **非公開でローカル。** ZIP、データベース、エクスポート、一時アップロード、Web UI、ログは自分のコンピューターに保存されます。
- **直接・差分インポート。** 公式 ZIP や展開済み JSON シャードを結合せずに読み込み、既存アーカイブを安全に更新できます。
- **読みやすく検索しやすい。** チャット形式リーダー、クラシック表示、フィルター、フレーズ、除外、ページング、任意の高速インデックスを利用できます。
- **持ち運べる出力。** 再現可能な Markdown/TXT をバックアップ、オフライン利用、移行に使えます。

## スクリーンショット

スクリーンショットには合成会話だけを使い、実在するタイトル、メッセージ、ファイル名、メールアドレス、ローカルパスを表示しないでください。

## このプロジェクトでできること

- OpenAI / ChatGPT のエクスポート ZIP、単体の `conversations.json` ファイル、または展開済みのエクスポートディレクトリから `conversations.json` と sharded `conversations-*.json` を SQLite に取り込みます。
- 会話メタデータ、mapping nodes、メッセージの role、本文テキスト、タイムスタンプ、親ノード関係、ソース追跡、インポート警告を保存します。
- 増分インポートに対応します。新しいエクスポートを同じデータベースへ再インポートすると、変更された会話を更新し、未変更データを意図的に重複させません。
- CLI 検索用に任意の FTS5 メッセージインデックスを作成します。
- ブラウザー検索を高速化する任意の Web 部分文字列インデックスを作成します。
- Markdown、TXT、またはその両方にエクスポートできます。
- メッセージ本文を表示しない `verify`、`stats`、プライバシーに配慮した `inspect` を提供します。
- 既存データベースがなくても起動できるローカル Web UI を提供し、ブラウザーから ZIP を選んでインポートできます。
- ログを構造化されたコマンド出力から分離し、タイトル、snippet、raw JSON、メッセージ本文を記録しません。

## プライバシー

すべてローカルで動作し、Web UI は既定で `127.0.0.1` にバインドします。通常のインポートは元の ZIP を削除せず、ブラウザーアップロードはサーバー側の一時コピーを使います。データベースとエクスポートには非公開の会話内容が含まれるため、安全に保管してください。

## 必要条件

- Python 3.10 以上。Web の依存関係は Python 3.13 でテストされています。
- JSON 対応 SQLite。FTS5 は任意で、なくてもスキャン検索を利用できます。
- Node.js と npm は React Web UI の再ビルドまたはテスト時だけ必要です。
- CLI は Python 標準ライブラリを使います。Web UI とブラウザー ZIP アップロードには `requirements-web.txt` をインストールしてください。

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -r requirements-web.txt -c constraints-web-py313.txt
```

Windows PowerShell:

```bash
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -r requirements-web.txt -c constraints-web-py313.txt
```

Windows cmd.exe:

```bash
py -3 -m venv .venv
.venv\Scripts\activate.bat
python -m pip install -U pip
python -m pip install -r requirements-web.txt -c constraints-web-py313.txt
```

## クイックスタート

ChatGPT エクスポート ZIP をリポジトリの外に置き、最速で安全なインポートコマンドを実行します。このコマンドは入力ハッシュ計算を省略し、最後に FTS を一度だけ再構築します。大きなアーカイブでは、行ごとに FTS を保守するよりかなり高速です。

```bash
NEW_ZIP="$HOME/Downloads/chatgpt_export/chatgpt_export.zip"
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

Windows PowerShell での同等の書き方:

```bash
$env:NEW_ZIP = "$env:USERPROFILE\Downloads\chatgpt-export.zip"
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$env:NEW_ZIP" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

Windows cmd.exe での同等の書き方:

```bash
set NEW_ZIP=%USERPROFILE%\Downloads\chatgpt-export.zip
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "%NEW_ZIP%" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

ローカル Web UI を起動します。

```bash
python chatgpt_archive.py web --db archive/chatgpt_archive.db --port 8787
```

データベースがまだ存在しない場合でも、Web UI は起動し、空状態とインポートパネルを表示します。ブラウザーで ChatGPT エクスポート ZIP を選ぶと、バックエンドがローカルの一時コピーへ書き込み、インポート後に `verify`、`stats`、`web-index` を自動実行します。

```bash
python chatgpt_archive.py web --port 8787
```

## よく使う CLI ワークフロー

チャット内容を表示せずにエクスポートを検査します。

```bash
python chatgpt_archive.py inspect --input "$NEW_ZIP"
```

空のデータベースを明示的に作成します。

```bash
python chatgpt_archive.py init --db archive/chatgpt_archive.db
```

大規模アーカイブ向けの経路でインポートします。

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
```

`--input` には公式エクスポート ZIP、単体の `conversations.json`、または展開済みのエクスポートディレクトリを指定できます。展開済みディレクトリには `conversations.json`、または sharded `conversations-*.json` ファイルを含められます。shard を手作業で結合しないでください。

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input conversations.json --no-input-sha256 --rebuild-fts
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input ./extracted-export/ --no-input-sha256 --rebuild-fts
```

構造上の整合性を確認します。

```bash
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
```

構造化された件数と期間境界を表示します。

```bash
python chatgpt_archive.py stats --db archive/chatgpt_archive.db
```

CLI 検索経路でメッセージ本文を検索します。表示されるのは conversation ID、node ID、role で、snippet は表示されません。

```bash
python chatgpt_archive.py search --db archive/chatgpt_archive.db --limit 20 "python sqlite"
```

会話を Markdown、TXT、または同じ実行内で両方の形式としてエクスポートします。`--format md` は Markdown 本文ファイルを書き出して manifest を更新し、`--format txt` は plain text 本文ファイルを書き出して manifest を更新し、`--format all` は両方の本文形式を書き出して manifest を更新します。

```bash
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format md --out exports
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format txt --out exports
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format all --out exports
```

日付範囲を指定し、必要なら既存ファイルを書き直します。`--from` と `--to` の日付境界は `YYYY-MM-DD` だけを受け付けます。

```bash
python chatgpt_archive.py export --db archive/chatgpt_archive.db --format md --out exports --from 2024-01-01 --to 2024-12-31 --force
```

任意の Web 検索インデックスを再構築します。

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
```

Web UI を起動します。

```bash
python chatgpt_archive.py web --db archive/chatgpt_archive.db --port 8787
```

## インポートモード

大規模アーカイブでは次のコマンドを推奨します。

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts
```

インポート後に SQLite の planner statistics と FTS インデックスをさらに整理したい場合は、次を使います。

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts --optimize-after-import --optimize-fts-after-import
```

`--delete-input-on-success` は互換性のため残されていますが利用できず、インポート前に失敗します。通常のインポートは元の ZIP を削除しません。データベース検証後に自分で削除してください。

```bash
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --no-input-sha256 --rebuild-fts --delete-input-on-success
```

## Web UI ワークフロー

次のコマンドでローカル Web UI を起動します。既存データベースを開くことも、データベースなしで起動してブラウザーから ZIP をインポートすることもできます。リーダーはページング対応のチャット/クラシック表示、検索、内部メッセージ制御、コピー、ダウンロードを提供します。

```bash
python chatgpt_archive.py web --port 8787
```

## Web アップロードの安全制限

ブラウザーアップロードはインポート前に検査されます。ループバックでは下表の大容量ローカル既定値を使います。非ループバックアクセスは明示的な有効化が必要で、信頼済みローカルプロファイルを選ばない限り小さい安全な既定値が適用されます。CLI `import` はこの制限の対象外です。

| 環境変数 | ローカルデフォルト | 制御対象 |
|---|---|---|
| `CHATGPT_ARCHIVE_MAX_UPLOAD_BYTES` | 20 GiB | 圧縮 ZIP アップロードの総サイズ |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_JSON_MEMBER_BYTES` | 64 GiB | 単一 JSON member の最大非圧縮サイズ |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_JSON_MEMBERS` | 5,000 | conversation JSON member の最大数 |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_TOTAL_UNCOMPRESSED_BYTES` | 128 GiB | 非圧縮 JSON データ総量の上限 |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_COMPRESSION_RATIO` | 1,000.0 | 大規模 JSON member の最大圧縮率 |
| `CHATGPT_ARCHIVE_MAX_UPLOAD_TOTAL_MEMBERS` | 100,000 | ZIP 内の総 member 数の上限 |
| `CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE` | unset | 信頼できる非 loopback ネットワークでのみ `local` に設定し、未設定のアップロード制限にローカル既定値を使う |

正規の大規模アーカイブのためにローカル制限を引き上げるには、Web UI 起動前に対応する変数を設定します：

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

信頼できる内部ネットワークで明示的な圧縮 ZIP 上限だけを上げ、他の未設定制限を remote-safe のままにするには：

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

信頼できる内部ネットワークで完全なローカルアップロード profile を使うには：

```bash
# macOS / Linux
export CHATGPT_ARCHIVE_REMOTE_UPLOAD_PROFILE=local
python chatgpt_archive.py web --host 0.0.0.0 --port 8787
```

## 検索構文

検索では通常語、引用フレーズ、大文字の `OR`、`-term` 形式の除外、`role:user`、`source:zip`、`path:current`、`path:all`、`scope:title`、`scope:message` などのフィルターを使えます。クエリは生 SQL ではなく検索文字列です。

日付フィルターとエクスポート期間は UTC の暦日を使います。ブラウザーの Cmd/Ctrl+F は描画済み行だけを対象にするため、未読込内容にはアーカイブ検索または会話コピーを使ってください。

```bash
python chatgpt_archive.py search --db archive/chatgpt_archive.db "python sqlite"
python chatgpt_archive.py search --db archive/chatgpt_archive.db "\"exact phrase\""
python chatgpt_archive.py search --db archive/chatgpt_archive.db "role:user path:current python -pandas"
```

Web 検索は `web-index` が作成する任意の normalized trigram インデックスを使います。ブラウザーで実用的な部分文字列検索を行うためのものです。これらの任意インデックスが存在しない、または壊れている場合は再構築してください。

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
```

## 検証と任意 Web インデックス

インポート後に `verify` を実行して SQLite と会話リンクを検査してください。

```bash
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
```

CLI FTS と Web 部分文字列インデックスは任意です。`verify` が任意 Web インデックスのエラーを報告した場合は、再構築してもう一度検証してください。

```bash
python chatgpt_archive.py web-index --db archive/chatgpt_archive.db
python chatgpt_archive.py verify --db archive/chatgpt_archive.db
```

## ログ

ログレベルは `debug`、`info`、`warning`、`error`、`none` で、既定は `warning` です。ログには状態と件数だけを記録し、会話タイトル、メッセージ、抜粋、生 JSON は含めません。

ログフラグはサブコマンドの前にも後にも置けます。

```bash
python chatgpt_archive.py --log-level debug web
python chatgpt_archive.py web --log-level debug
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --log-level info --log-file logs/import.log
python chatgpt_archive.py import --db archive/chatgpt_archive.db --input "$NEW_ZIP" --json-logs --log-file logs/import.jsonl
```

## ソースからビルド

次のコマンドはプロジェクト検査、Web UI のビルド、生成物の削除、実行可能配布物の検査に使います。

```bash
python -m compileall chatgpt_archive.py chatgpt_export_archiver tests tools
python -m unittest discover -s tests -v
python tools/clean_generated_artifacts.py --fail-on-blocked
python tools/check_delivery_clean.py --mode runnable .
```

Web UI をビルドし、smoke test を実行します。

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

ZIP 配布物を確認する場合:

```bash
python tools/check_delivery_clean.py --mode runnable path/to/delivery.zip
```

## パッケージ内容

実行可能パッケージには Python ソース、テスト、文書、Web の依存関係とソース、ビルド済み `webui/dist` を含めます。データベース、エクスポート、ZIP、ログ、キャッシュ、`node_modules`、その他のローカルデータは含めないでください。

## データベース概要

SQLite には会話、メッセージ、インポート履歴、警告、任意検索インデックスが保存されます。`database_migration_required` が表示された場合はバックアップを作成してから `python chatgpt_archive.py migrate --db archive/chatgpt_archive.db` を実行してください。

## 既知の制限

- これはローカルアーカイブツールであり、クラウド同期サービスではありません。
- Web UI はローカル利用を想定しています。独自のアクセス制御なしに信頼できないネットワークへ公開しないでください。
- エクスポート解析は、現在確認されている OpenAI / ChatGPT のエクスポート形式に従います。上流の形式が変わった場合は、新しいインポート経路を信頼する前に `inspect` とテストを更新してください。
- エクスポートファイル名の部品は Windows と Unix 系の両方に向けてサニタイズされます。`CON`、`AUX`、`COM1`、`LPT9`、`COM¹`、`LPT²` などの予約デバイス名、末尾のドットや空白も対象です。
- 非常に大きなアーカイブでは、インポート、FTS 再構築、Web trigram インデックス作成に時間がかかることがあります。大規模インポートでは `--rebuild-fts` 経路を優先してください。

## 安全上の注意

インポート、アップロード、検索、コピー、エクスポート、データベース書き込みでは、入力に上限を設け、内容を含まない安全なエラーを返します。ネットワークを信頼できない場合は Web UI をループバックだけで実行し、検証が成功するまで元の ZIP を残し、データベース、一時ファイル、WAL、インデックス用の十分な空き容量を確保してください。
