# Existing Web UI demo bundle

[Download the synthetic demo bundle](https://github.com/quangnd/chatgpt-sqlite-webui/raw/refs/heads/poc/archive-evaluation/poc/ui-preview/chatgpt-ui-preview.zip)

If GitHub opens its file page, click **Download raw file**. Extract the ZIP and open
`chatgpt-ui-preview/START-HERE.md` for local startup instructions, or inspect the
10 real browser screenshots under `screenshots/`.

This review artifact includes 72 wholly synthetic conversations, a consistent
SQLite read database, the original synthetic export ZIP, browser-check evidence,
and a usability review. It is a demo bundle, not an application release package.
The read DB uses DELETE journal mode and requires no WAL/SHM sidecars.

The bundle was added after the original workspace-only download link proved
unusable. No application/UI source, dependency lockfile, or default branch changed.
No service was deployed. The original preview review describes the earlier preview
run; this subsequent commit only makes its artifacts downloadable through GitHub.

The outer checksum is in `chatgpt-ui-preview.sha256`; the ZIP also includes a
SHA256.json inventory of its payload files.
