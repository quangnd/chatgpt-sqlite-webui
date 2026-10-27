# Snapshot and recovery evaluation controller

Run from the repository root with Python 3.12+ on Linux. The Python code uses only
the standard library and invokes the unchanged project CLI in a child process.
Optional encryption invokes installed GnuPG and its supervised local agent. No
network, accounts, production services, or application changes are needed.

Use synthetic data and private, trusted local directories. This POC is not a
production archiver. Read `poc-report-phase2.md` before interpreting its results.

```bash
python -m poc.archive_controller --root /tmp/example-active preserve /tmp/synthetic-export.zip
python -m poc.archive_controller --root /tmp/example-active import SNAPSHOT_SHA256 --after none
python -m poc.archive_controller --root /tmp/example-active status
python -m poc.archive_controller --root /tmp/example-active verify
python -m poc.archive_controller --root /tmp/example-active preserve /tmp/synthetic-export-updated.zip
python -m poc.archive_controller --root /tmp/example-active import UPDATED_SHA256 --after SNAPSHOT_SHA256
python -m poc.archive_controller --root /tmp/example-active backup /tmp/example-backup
python -m poc.archive_controller --root /tmp/example-restored restore /tmp/example-backup
python -m poc.archive_controller --root /tmp/example-active rebuild /tmp/example-rebuilt.db
```

Substitute IDs returned by `preserve`; IDs are SHA-256 hex strings. Paths above are
examples, not directories created by documentation. Backup/restore/rebuild refuse
existing destinations and nested source/destination paths. A new backup generation
needs a new destination. Exit 2 means rejected, partial, or invalid; no warning
bearing import is published, even if the application exited 0.

Optional encrypted workflow (supply a private passphrase file; never place its
contents in a command, report, or Git):

```bash
python -m poc.archive_controller --root /tmp/example-active seal /tmp/example-backup /tmp/example-backup.gpg --key-file /tmp/example-private-passphrase
python -m poc.archive_controller --root /tmp/example-decrypted unseal /tmp/example-backup.gpg --key-file /tmp/example-private-passphrase
```

`seal` packages an already completed consistent backup, then delegates AES256
symmetric encryption and integrity protection to GnuPG. `unseal` requires successful
GnuPG decryption before bounded regular-file unpacking, SHA comparison, SQLite
integrity/foreign-key checks, application verify, and directory publication. A
successful seal alone does not establish recovery. The lifecycle test actually
unseals, compares inventories, and compares canonical rows. Passphrase loss means
backup loss. Plaintext active files, backup directory, tar staging and decrypted
staging remain private plaintext; encryption does not secure those copies.

## Storage and publication

- `objects/<sha256>.zip`: exact original bytes, exclusive hard-link publication,
  fsync, mode 0400, never overwritten or deleted by the controller.
- `manifest.json`: version 1, snapshot ID/hash/size/acquisition UTC time, validation
  error/status, import order and safe attempt outcome/counts/warning codes. The
  `database` pointer and `head` form the sole atomic current-view publication.
- `databases/<generation>.db`: consistent SQLite backup of an admitted staging DB.
  Readers resolve the manifest pointer once when opening. There is no mutable
  `current.db` alias. Reopen the reader to see a newly admitted generation.
- `work/`: disposable copies and failed/interrupted import DBs retained for review.
  Old DB generations also remain. No automatic retention or cleanup job exists.
  Successful original-copy staging is unlinked only after its canonical hard link
  is durable; duplicate disposable copies are discarded. Original input paths and
  canonical object paths are never renamed/unlinked.

The Linux flock serializes controller operations; all writers must use this
controller. Published DB generations must be treated as read-only by operators.
Read-only permission bits are an accidental-write guard, not WORM or protection
against the owning account/root. Filesystems must support local flock, hard links,
atomic rename and fsync. Network filesystems and hostile/shared directories are
outside this proof. Entirely new orphan original objects can remain if a crash
occurs between object and manifest publication; they are never removed or silently
overwritten, and resubmitting the same ZIP can register the existing valid object.

## Admission and replay policy

1. Preservation is independent of reader admission. Full ZIP CRC validation streams
   every member, including attachments; malformed/rejected bytes stay preserved.
   The POC caps ZIP entries at 100,000 and declared expanded bytes at 512 MiB.
   Existing application limits also apply. ZIPs are never extracted for import.
2. A submission with identical bytes has one snapshot ID and no new import. A
   different ZIP with identical conversations is a different preserved snapshot.
3. First admission requires `--after none`; later admissions require the exact head
   SHA. Acquisition time and ZIP metadata do not decide freshness.
4. Start from an online SQLite backup of the current admitted DB and import using
   the original CLI, in isolation. Missing whole conversations remain. Inspect
   latest-run status/counts/warnings, safe CLI warning signals and verify results.
   Any warning, skipped element, stderr output or failed verify blocks publication.
5. Compare every existing conversation's timestamp, aggregate hash, title, creation
   time, current node, flags, model, metadata and exported ID. Changed existing
   states require both finite update timestamps and a strictly larger new value.
   Unknown/equal/decreasing times with changes reject the whole admission. New IDs
   are accepted. Unchanged existing states are allowed. This is deliberately
   conservative and does not prove timestamps truthful or globally merge history.
6. Import-order integers are allocated before attempts; failed attempts leave gaps.
   Only successful `import_order` values enter reconstruction. Failed/partial/old
   snapshots can be examined using the project CLI against a separate fresh DB;
   they never enter current replay. There is no override/complex merge engine.
7. A killed controller's durable `importing` record becomes `interrupted` on the
   next operation. Its unpublished DB is ignored. Failed snapshots can be retried.
   Duplicate already-admitted snapshots do not replay over a later head.
8. `rebuild` replays admitted original ZIPs in recorded order into a fresh DB and
   verifies it. It works with a missing active DB and leaves the manifest untouched.
   Use the resulting DB as a separate reader, or restore a complete backup into a
   new archive root. This POC has no automatic repair/promotion command.

Backup locks the controller, copies every canonical original (including rejected
snapshots), writes the manifest, uses `sqlite3.Connection.backup` for only the
referenced current generation, verifies it, and creates a SHA-256 inventory and
completion marker. A hidden incomplete directory is renamed only after completion.
Restore compares the entire inventory before and after copying and verification,
validates every original's hash/size, and publishes only a verified new directory.
Old read generations/work are not backed up; all original snapshots are. Hash
inventories detect accidental damage, not malicious replacement of both data and
manifest. Code is not bundled; retain this Git revision separately for replay.

## Reproduce tests

```bash
PHASE2_EVIDENCE=/tmp/phase2-evidence PYTHONPATH=poc python poc/measure.py /tmp/phase2-suite python poc/reap_run.py python -m unittest test_archive_lifecycle test_archive_poc -v
PHASE2_EVIDENCE=/tmp/phase2-scale PYTHONPATH=poc python poc/measure.py /tmp/phase2-scale-run python poc/reap_run.py python -m unittest test_archive_lifecycle.Lifecycle.test_scale_1000_conversations_lifecycle -v
```

Run with `PYTHONTRACEMALLOC` unset for resource measurements. Existing
`poc/reap_run.py` is a Linux subreaper for this container's non-reaping PID 1.
Tests generate and remove only owned disposable synthetic fixtures. Fault seams
are injected Python callbacks or mocks, not production CLI options. Evidence
includes failures from development; final results refer to the final code.
