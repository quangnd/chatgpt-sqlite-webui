# ChatGPT Archive Technical POC

## 1. Executive summary and recommendation

**Use this project as a rebuildable local reader/search database, with immutable original ZIP snapshots as the preservation authority. Do not use its SQLite database alone as a lossless, historical archive.** This is a conditional reuse recommendation, not a deployment approval.

Reviewed commit: `d558b965bfbf288dd1e32323edfdf249c8b6a6d8`, branch `poc/archive-evaluation`. All fixtures are synthetic. No account access, personal conversations, external API calls, deployment, production connection, product changes, merge, or PR was performed. The existing development service from onboarding was not repurposed for production.

Verified: duplicate imports retain canonical content without duplicating nodes; missing whole conversations remain; changed conversations replace their own state; removed nodes disappear; older exports can overwrite newer state. Branches present in an export survive import. SQLite backup and fresh reconstruction from retained originals work for the small fixtures tested. Message JSON survives semantically, but the DB does not preserve the entire original export or attachment bytes.

For a lightweight Linux server, retain snapshots and use CLI ingestion; enable the optional Web UI only through a loopback connection/SSH tunnel when needed. Avoid maintaining a parser fork during this POC. A minimal new archiver is attractive only if immutable per-snapshot revision queries are a firm MVP requirement; that requirement needs explicit storage design and tests rather than simply extracting this parser.

## 2. Verified source findings

References below are repository-relative and refer to the reviewed commit. New POC test references refer to this branch.

| Area | Evidence | Implication |
|---|---|---|
| Canonical schema | [db.py:49](chatgpt_export_archiver/db.py#L49), [db.py:82](chatgpt_export_archiver/db.py#L82), [db.py:99](chatgpt_export_archiver/db.py#L99) | Run ledger, current conversation row and `(conversation_id,node_id)` nodes; no per-snapshot revision key. Parent/child graph relationships are stored, but are not enforced by parent-node foreign keys. |
| Branch preservation | [parser.py:145](chatgpt_export_archiver/parser.py#L145), [parser.py:156](chatgpt_export_archiver/parser.py#L156), [parser.py:398](chatgpt_export_archiver/parser.py#L398) | Iterates all mapping entries; selected current ancestry is an additional flag, not the only stored branch. Invalid non-object nodes are skipped with warnings. Missing/cyclic ancestry has diagnostic paths. |
| Edits and removals | [db.py:2698](chatgpt_export_archiver/db.py#L2698), [db.py:2714](chatgpt_export_archiver/db.py#L2714), [db.py:2776](chatgpt_export_archiver/db.py#L2776) | Existing nodes synchronize to incoming nodes, including DELETE for removed identities; upsert overwrites conversation state. No historical node table. POC verifies disappearance of an old branch and replacement of text. |
| Older imports | [db.py:2776](chatgpt_export_archiver/db.py#L2776) | No update-time monotonic guard in this upsert. POC verifies older ZIP imported later restores older content in the same DB. Run IDs/hashes are provenance, not reversible deltas. |
| Raw and metadata | [parser.py:178](chatgpt_export_archiver/parser.py#L178), [parser.py:217](chatgpt_export_archiver/parser.py#L217), [parser.py:232](chatgpt_export_archiver/parser.py#L232) | Raw **message object** is serialized; node metadata is selected; conversation metadata uses a whitelist. Arbitrary top-level/node keys and original JSON formatting are not preserved. POC verifies missing sentinel fields. |
| Multimodal projection | [parser.py:437](chatgpt_export_archiver/parser.py#L437), [parser.py:498](chatgpt_export_archiver/parser.py#L498) | Generic recursive text extraction/labels/placeholders; depth-limited display projection is not a faithful media representation. Raw image pointer and custom message metadata survived POC; actual audio/video/image rendering was not tested. |
| Attachments | [db.py:131](chatgpt_export_archiver/db.py#L131), [db.py:2306](chatgpt_export_archiver/db.py#L2306), [db.py:2327](chatgpt_export_archiver/db.py#L2327) | File inventory stores paths/type/size; per-entry hashes and relationship columns are inserted NULL. No attachment payload column. Synthetic binary member was inventoried, not ingested as a binary archive. A remote file pointer is not a stored attachment. |
| Import atomicity | [cli.py:935](chatgpt_export_archiver/cli.py#L935), [cli.py:1298](chatgpt_export_archiver/cli.py#L1298), [cli.py:1358](chatgpt_export_archiver/cli.py#L1358), [cli.py:1505](chatgpt_export_archiver/cli.py#L1505) | Data transaction, finalize/commit and exception rollback; failed-run status is persisted separately. Truncated JSON POC leaves previous canonical rows unchanged and verify passes. Schema initialization/run-ledger work means failure need not leave a byte-identical database. |
| Strict files, tolerant elements | [scanner.py:1064](chatgpt_export_archiver/scanner.py#L1064), [parser.py:74](chatgpt_export_archiver/parser.py#L74), [cli.py:454](chatgpt_export_archiver/cli.py#L454) | Streaming list framing; bad elements can be skipped while the command succeeds; decoding/CRC failures reject the import. Operators must inspect warning and committed/skipped counts, not exit code alone. |
| Ambiguity | [scanner.py:1022](chatgpt_export_archiver/scanner.py#L1022), [cli.py:916](chatgpt_export_archiver/cli.py#L916) | Duplicate source identities have rejection checks; repeated conversation IDs use last-wins with warning. A successful import can contain semantic conflicts. |
| Bounds | [scanner.py:26](chatgpt_export_archiver/scanner.py#L26), [scanner.py:70](chatgpt_export_archiver/scanner.py#L70), [parser.py:15](chatgpt_export_archiver/parser.py#L15), [resource_contract.py:5](chatgpt_export_archiver/resource_contract.py#L5) | 32 MiB input/character element limits, 5,000 imported nodes per conversation, token/heap/depth and batch limits. Intentional rejection/skipping protects resources but prevents universal export completeness. Large legitimate inputs must remain available in originals. |
| SQLite configuration | [db.py:738](chatgpt_export_archiver/db.py#L738), [db.py:1016](chatgpt_export_archiver/db.py#L1016) | Foreign keys enabled; bulk tuning requests WAL, synchronous NORMAL, ~256 MiB cache and mmap each. These are configuration targets, not measured resident memory or proof of power-loss durability. NORMAL can lose recent commits on power failure; it is not an archival replication guarantee. |
| Schema evolution | [db.py:1504](chatgpt_export_archiver/db.py#L1504), [db.py:1770](chatgpt_export_archiver/db.py#L1770) | Explicit locked transactional migration and rollback paths. Back up before upgrades; pin code/schema versions. Source review is not fault-injection proof for every migration. |

Existing regression coverage includes duplicate IDs, metadata-only updates, malformed shards/rollback, optional-index recovery, schema migrations, readonly behavior, locking, resource limits and Web boundary tests. Examples: [test_archiver.py:1522](tests/test_archiver.py#L1522), [test_archiver.py:2648](tests/test_archiver.py#L2648), [test_archiver.py:3561](tests/test_archiver.py#L3561), [test_archiver.py:4162](tests/test_archiver.py#L4162), [test_archiver.py:4919](tests/test_archiver.py#L4919). A test name alone is not proof of its broadest possible scenario.

## 3. Execution evidence and reproduction

Environment: Linux x86_64, glibc 2.41, CPython 3.12.14, SQLite 3.53.1; existing Python 3.12 constrained Web dependencies. No new dependency was installed. Node/Chromium were already available; frontend checks from onboarding are historical evidence, not rerun as part of this archive POC.

Run from `/workspace/chatgpt-sqlite-webui`:

```bash
mkdir -p /workspace/poc-evidence
/workspace/.venvs/chatgpt-sqlite-webui/bin/python poc/measure.py /workspace/poc-evidence/existing-retry /workspace/.venvs/chatgpt-sqlite-webui/bin/python poc/reap_run.py /workspace/.venvs/chatgpt-sqlite-webui/bin/python -m unittest discover -s tests -v
/workspace/.venvs/chatgpt-sqlite-webui/bin/python poc/measure.py /workspace/poc-evidence/poc /workspace/.venvs/chatgpt-sqlite-webui/bin/python -m unittest discover -s poc -p 'test_*.py' -v
```

`poc/reap_run.py` is an external Linux subreaper, used because this container's PID 1 does not reap orphans. It preserves command exit status and does not change application code or assertions. On a normal host init this accommodation should be unnecessary. `poc/measure.py` captures command, exit status, wall time and child rusage. Initial attempt to use `/usr/bin/time -v` returned 127 because that executable is absent; it ran no tests. Standard-library measurement replaced it.

The first existing-suite run invoked the same subreaper from its onboarding path. It executed 491 tests in 81.896 seconds: **488 passed, 1 error, 2 skipped**, command exit 1. The error was `ValueError` reading an empty PID marker in `test_real_pipeline_shutdown_proves_owned_grandchild_cleanup`, [test_resource_security_regressions.py:640](tests/test_resource_security_regressions.py#L640). The test waits for file existence but does not wait for its contents. The isolated retry passed (see evidence); full rerun outcome is recorded below. This is a test synchronization defect, not evidence of lost conversation data.

The first POC run executed 11 cases: 9 passed, 2 assertion failures because the new harness expected exit 1 rather than the CLI's documented-by-code exit 2 ([cli.py:232](chatgpt_export_archiver/cli.py#L232)). Corrected only those expectations. Final run: **11 passed, 0 skipped, exit 0**, 1.623 seconds reported by unittest. Failed input subprocesses returned 2 as expected; successful import/verify subprocesses returned 0. No application fix was needed.

| POC case | Verified result |
|---|---|
| Initial ZIP | 1 conversation / 3 nodes; source ZIP unchanged; CLI verify succeeds |
| Same ZIP twice | Canonical rows identical; 2 run-ledger entries, no node duplication |
| Updated conversation | Edited text replaces previous text; missing branch removed, 2 nodes remain |
| Missing whole conversation | Both conversations remain after importing only one |
| Multiple branches | Both leaves stored; only root and selected leaf flagged current |
| Bad elements in valid list | Null and integer skipped with warnings; valid conversation committed |
| Truncated JSON | Exit 2; pre-existing canonical rows identical; verify succeeds |
| Selected member CRC damage | Exit 2 with `zip_member_crc_failed` |
| Raw multimodal / metadata | Full message object equal after JSON decode; unknown conversation/node sentinels absent; attachment inventory exists |
| SQLite backup / restore | Standard-library `Connection.backup()`; canonical equality, integrity_check `ok`, CLI verify succeeds |
| Original snapshot recovery | Fresh DB rebuilt from v1 matches selected v1 canonical fields; v1 ZIP SHA unchanged; importing v1 over v2 also downgrades current state |

Canonical equality here compares conversation ID/title/current node and node identity/parent/children/text/raw message fields. It is not a byte-identical DB comparison or an assertion that every original field survives. Tests clean their temporary synthetic ZIPs/DBs after collecting measurements. Command logs retain outputs and temporary path names for audit, not restorable fixture files; fixtures regenerate deterministically enough for these semantic contracts.

## 4. Defects and severity

Severity is relative to the proposed preservation MVP, not a claim that documented reader behavior is a product bug.

| Finding | Severity | Classification / action |
|---|---|---|
| DB-only historical loss on updates/removals | High | Verified design gap: revisions disappear. Retain immutable originals and versioned backups; do not describe run ledger as history. |
| Attachment bytes and arbitrary export fields absent from DB | High | Verified completeness gap. Preserve complete ZIP bytes; database-only backup is insufficient. No proof that remote pointers are resolvable. |
| Old snapshot can overwrite newer current state | Medium | Verified ingestion-policy risk. Serialize and order ingestion externally, record snapshot identity/order; replay to separate DB for historical views. |
| Successful import can skip data | High if unnoticed | Intentional resource/validation boundary, not silent universal success. Require acceptance review of warnings and counts; retain originals even on rejection. |
| PID marker read race | Low | Observed existing-test defect; isolated retry passes. No code change in this POC. |
| WAL NORMAL durability window | Medium | Source-established risk, not reproduced power-loss corruption. Originals plus tested backups are required; evaluate FULL if latest-commit durability is mandatory. |

No new corruption defect in the application pipeline was demonstrated by these small cases. This is not proof of absence of defects.

## 5. Archive MVP gaps and security/operations

Assumed MVP: preserve full source bytes, all available branches/media references and metadata, keep revisions, detect corruption, recover after server loss, maintain encrypted local backups, and permit private read/search at low cost. Revision and backup requirements follow the task; no RPO, RTO, data volume, retention period or hardware budget was supplied.

The project provides current-state browsing/search, CLI validation, original-input preservation and rebuildable indexes. It does not, in the paths reviewed/tested, supply a complete immutable snapshot repository, versioned attachment storage, encrypted backup scheduling/key recovery, off-host restore automation, or per-snapshot historical query model. Retaining originals makes those gaps manageable without product changes. Markdown/TXT are convenience derivatives, not complete recovery sources; default current-path exports can omit branches/internal messages.

Security: keep the UI loopback-only. [web_app.py:269](chatgpt_export_archiver/web_app.py#L269) validates Host; [web_app.py:386](chatgpt_export_archiver/web_app.py#L386) applies write-origin policy; [web_app.py:520](chatgpt_export_archiver/web_app.py#L520) defaults to loopback. These controls are not an authenticated multiuser internet service. Do not infer production exposure safety. Use SSH forwarding rather than adding a public bind. Archive DBs and backups contain plaintext private content before encryption: restrict filesystem access, logs, temporary uploads and backup staging. SQLite itself is not encrypted by this project.

CLI uses standard-library SQLite/ZIP/JSON; optional Web uses FastAPI, Uvicorn, python-multipart and HTTPX (exact top-level versions in requirements-web.txt). React/Vite/TypeScript and npm are build-time dependencies when changing UI; prebuilt static assets ship. Linux Python 3.12 constraints are version pins, not a Linux artifact hash lock; retained macOS wheel hashes do not validate Linux wheels. No API keys, account sessions, Docker or database server are needed. Pin a reviewed revision and review dependencies before upgrades.

ZIP scans have ambiguity/duplicate and resource guards; the POC exercised selected JSON CRC damage, not every member's checksum. File inventory does not verify attachment content individually. Full ZIP integrity checks and SHA-256 snapshot manifests belong in the preservation workflow. Hashes detect damage only when a trusted manifest/backup exists; they are not encryption or independent authenticity proof.

## 6. Resource measurements and limits

Measurements are single synthetic runs on the cloud machine, not performance promises for a lightweight server. Child `ru_maxrss` on Linux is the largest child-process high-water RSS reported through wait/rusage, including descendants where propagated; it is **not simultaneous summed process-tree RSS** and excludes unrelated services. Wall timing includes startup and harness overhead. DB sizes are end-of-test file sizes, not peaks. WAL/SHM are measured separately where shown.

- Initial ZIP: 1,173 bytes, including a synthetic binary member.
- Initial database: 118,784 bytes; SHM 32,768 bytes; WAL 0 bytes at measurement.
- SQLite backup: 118,784 bytes, equal to measured source DB size.
- Tiny-fixture DB/ZIP ratio: ~101.3x, dominated by schema/page/index overhead. Do not extrapolate it to real archives.
- Final 11-case POC: command wall 1.693 seconds; max child RSS 21,888 KiB; child user/system CPU 1.457 / 0.238 seconds.
- First existing suite: command wall 82.351 seconds; max child RSS 361,688 KiB; child user/system CPU 71.784 / 3.345 seconds. This is a suite measurement, not importer steady-state memory.

No sustained daemon RSS/CPU, representative archive throughput, compressed-backup ratio, encrypted transfer cost, concurrent-writer backup, large attachment recovery, disk peak, power-loss durability, hardware-failure recovery, or multi-GiB scale matrix was measured. Two opt-in suite cases remain skipped: million-conversation allocation acceptance and giant-raw exact-verifier acceptance. There is no measured server sizing recommendation. Disk must hold originals, DB/indexes, WAL/temporary staging and local backup staging; test with representative synthetic workloads before selecting small hardware.

## 7. Build versus reuse

| Option | Maintainability / dependencies | Storage and preservation | Operations / recommendation |
|---|---|---|---|
| Reuse full project | Broad existing tests and working UI; substantial search/security/index/migration surface. Python CLI simple to run; optional Python Web stack plus frontend toolchain to maintain. | Raw-message plus normalized/index data duplicates some content; immutable originals still essential; current DB is not revision history. | Lowest immediate implementation cost. Preferred conditional option: pinned CLI + optional local UI + external snapshot/backup discipline. |
| Reuse import/parser components | Reduces UI scope but scanner/parser/schema/resource contracts and normalization are coupled; extraction creates API/fork maintenance burden. Must retain limits/warnings and tests. | Current parser output already omits fields; transplanting it does not create lossless preservation or revisions. | Choose only with a defined integration boundary and ownership budget. No compelling advantage for this POC. |
| Minimal Python + SQLite archiver | Standard library can keep runtime dependencies low; new ZIP safety, bounds, identity, migration and recovery code would need extensive testing. Smaller UI-free scope, not automatically safer. | Design immutable snapshots and original bytes first; raw per-snapshot JSON and attachments add storage but make replay/history explicit. Deduplicate blobs only after correctness. | Higher initial correctness cost; plausible if revision queries are mandatory and UI/search breadth is unnecessary. Do not replace a tested parser casually. |

## 8. Proposed minimal architecture (design only)

1. **Immutable originals:** local server directory/object store with SHA-256-addressed complete ZIPs, snapshot acquisition timestamp and explicit ingestion sequence. Never overwrite originals. Check full ZIP member integrity, record manifest/hash/size and parser/code version. Mark rejected/partial snapshots explicitly. Original bytes remain the authority.
2. **Snapshot catalog:** SQLite `snapshots(id, sha256 UNIQUE, received_at, byte_size, validation_status, parser_version)` and import outcome/count/warning records. If building a new revision-aware store, key conversations/nodes by snapshot ID and logical IDs, retaining complete raw conversation/node objects, graph edges, and media references. Store attachment blobs separately by hash plus per-snapshot manifest/path mapping. These are proposals, not implemented tables.
3. **Rebuildable read model:** current project's DB, created from explicitly ordered selected originals with serialized ingestion. Keep current-state selection separate from snapshot history. Derive text/search indexes; remove/rebuild optional indexes without risking originals. Reconstruct an exact historical snapshot in a fresh DB rather than merging older inputs into the live view.
4. **Consistent backups:** use SQLite online backup API (tested) or a documented quiesced alternative, never blindly copy only the main file of a live WAL DB. Validate backup with integrity/foreign-key checks and project verify. Back up **both originals/manifests and DB**, and preserve pinned code/schema information. Backups of the DB alone cannot recover discarded metadata/media/history.
5. **Encrypted local copies:** proposed established authenticated encryption/backup tool, e.g. an encrypted restic repository on the local computer or age-encrypted consistent snapshot bundles transferred over SSH. Neither tool was installed or tested. Keep encryption keys/recovery instructions separately recoverable, retain multiple generations, and verify a completed backup before pruning. Incremental encrypted backup is likely to reduce transfer/storage cost; no cost was measured.
6. **Recovery drill:** on a separate directory/host, restore encrypted originals/manifests and consistent DB, verify hashes and SQLite/project health, and compare snapshot counts. Also rebuild a fresh DB from a chosen original snapshot; test attachments directly from originals. Record recovery time and data-loss window against a user-selected RPO/RTO. Encryption/decryption and off-host recovery remain acceptance work.

Proposed acceptance gate before adoption: representative synthetic volume/media fixtures; completed encrypted off-host restore; warning-review policy; ordered snapshot retention; available disk/peak resource measurement; explicit RPO/RTO. No deployment is recommended until these are demonstrated.

## Final run evidence

Full rerun: **491 tests executed, 489 passed, 2 skipped, exit 0**; unittest time 84.955 seconds, measured command wall 85.388 seconds, max child RSS 381,628 KiB, child user/system CPU 74.814 / 3.032 seconds. The isolated PID-marker retry executed 1 test and passed, exit 0; command wall 0.573 seconds, max child RSS 44,556 KiB. The earlier failure remains recorded rather than erased by the rerun.

Retained evidence (synthetic content only):

- [Full-suite first run metrics](poc/evidence/existing.json) and [output](poc/evidence/existing.txt).
- [Full-suite successful rerun metrics](poc/evidence/existing-retry.json) and [output](poc/evidence/existing-retry.txt).
- [Isolated retry metrics](poc/evidence/cleanup-retry.json) and [output](poc/evidence/cleanup-retry.txt).
- [POC final metrics](poc/evidence/poc.json) and [output](poc/evidence/poc.txt); [initial harness failure](poc/evidence/poc-first.txt) and [metrics](poc/evidence/poc-first.json).
- [Every POC subprocess command, output, exit code and runtime](poc/evidence/commands.json), including both runs; [size observations](poc/evidence/sizes.json), including repeated observations from both runs.
- [Synthetic test cases](poc/test_archive_poc.py), [measurement wrapper](poc/measure.py), [container subreaper](poc/reap_run.py).

No application source, existing tests, dependency declarations or lockfiles were edited. POC report/helpers/evidence are retained on the separate review branch; no application changes were made and nothing was merged. Evidence files use `.txt`/`.json` because the repository ignores local `.log`/`.jsonl` artifacts. They are POC review artifacts, not release-package additions.
