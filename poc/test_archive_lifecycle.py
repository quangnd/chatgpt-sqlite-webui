"""Synthetic Phase 2 integration and deterministic fault injection."""
import copy
import errno
import hashlib
import json
import os
from pathlib import Path
import random
import shutil
import signal
import resource
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
import zipfile
from unittest.mock import patch

from archive_controller.controller import Archive, Rejected, app, digest, inventory, restore, seal, unseal
from test_archive_poc import fixture

REPO = Path(__file__).resolve().parents[1]
EVIDENCE = Path(os.environ.get('PHASE2_EVIDENCE', '/tmp/archive-phase2-evidence'))
EVIDENCE.mkdir(parents=True, exist_ok=True)


def record(name, value):
    (EVIDENCE / (name + '.json')).write_text(json.dumps(value, indent=2, sort_keys=True) + '\n')


def treebytes(path):
    # Logical file lengths, not allocated blocks; exclude hard-link double counting.
    seen, size = set(), 0
    for p in path.rglob('*'):
        if p.is_file():
            stat = p.stat()
            key = stat.st_dev, stat.st_ino
            if key not in seen:
                seen.add(key)
                size += stat.st_size
    return size


def canonical(db):
    with sqlite3.connect(db) as c:
        return (c.execute('SELECT conversation_id,title,current_node,aggregate_hash FROM conversations ORDER BY 1').fetchall(),
                c.execute('SELECT conversation_id,node_id,parent_node_id,children_json,content_text,raw_message_json FROM conversation_nodes ORDER BY 1,2').fetchall())


class Lifecycle(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='phase2-synthetic-')
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.a = Archive(self.base / 'active')

    def manifest(self, a=None):
        return json.loads(((a or self.a).root / 'manifest.json').read_text())

    def current(self, a=None):
        a = a or self.a
        return a.root / self.manifest(a)['database']

    def export(self, name, items=None, raw=None, attachments=None):
        p = self.base / (name + '.zip')
        with zipfile.ZipFile(p, 'x', compression=zipfile.ZIP_DEFLATED) as z:
            z.writestr('conversations.json', raw if raw is not None else json.dumps(items))
            for name, data in (attachments or {}).items():
                z.writestr(name, data)
        return p

    def preserve(self, p):
        before = digest(p)
        result = self.a.preserve(p)
        self.assertEqual(digest(p), before)
        self.assertEqual(digest(self.a.root / 'objects' / (result['id'] + '.zip')), before)
        return result['id']

    def load(self, name='initial', items=None):
        sid = self.preserve(self.export(name, [fixture()] if items is None else items))
        head = self.manifest()['head'] or 'none'
        result = self.a.admit(sid, head)
        return sid, result

    def test_cli_commands_and_partial_exit(self):
        commands = []
        def run(*args, expected=0):
            cmd = [sys.executable, '-m', 'poc.archive_controller', '--root', str(self.a.root), *map(str,args)]
            result = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, expected, result.stdout + result.stderr)
            value = json.loads(result.stdout)
            commands.append(dict(command=[str(x).replace(str(self.base), '<synthetic-temp>') for x in cmd],
                                 exit_code=result.returncode, result=value))
            return value
        sid = run('preserve', self.export('cli', [fixture()]))['id']
        run('import', sid, '--after', 'none')
        run('verify')
        partial = run('preserve', self.export('cli-partial', [None, fixture()]))['id']
        self.assertEqual(run('import', partial, '--after', sid, expected=2)['status'], 'partial')
        record('cli-commands', commands)

    def test_missing_conversations_retained(self):
        a, b = fixture(), fixture(); b['id'] = 'synthetic-b'
        self.load('both', [a,b])
        a['update_time'] = 10; a['title'] = 'synthetic update'
        self.load('one', [a])
        with sqlite3.connect(self.current()) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM conversations').fetchone()[0], 2)
        self.a.verify()

    def test_duplicate_idempotence_and_original_bytes(self):
        p = self.export('duplicate', [fixture()], attachments={'a.bin': bytes(range(256))})
        sid = self.preserve(p)
        self.assertEqual(self.a.admit(sid, 'none')['status'], 'imported')
        before = canonical(self.current())
        self.assertTrue(self.a.preserve(p)['duplicate'])
        self.assertTrue(self.a.admit(sid, 'none')['duplicate'])
        self.assertEqual(len(self.manifest()['snapshots']), 1)
        self.assertEqual(len(list((self.a.root / 'objects').glob('*.zip'))), 1)
        self.assertEqual(canonical(self.current()), before)
        self.a.verify()

    def test_ordering_explicit_head_and_per_conversation_regression(self):
        old = fixture()
        older_id = self.preserve(self.export('old', [old]))
        newer = copy.deepcopy(old)
        newer['update_time'] = 10
        newer['mapping']['b']['message']['content']['parts'] = ['updated']
        head, _ = self.load('new', [newer])
        before = canonical(self.current())
        with self.assertRaisesRegex(Rejected, 'explicit_predecessor'):
            self.a.admit(older_id, 'none')
        with self.assertRaisesRegex(Rejected, 'not_increasing'):
            self.a.admit(older_id, head)
        self.assertEqual(canonical(self.current()), before)
        self.assertEqual(self.manifest()['head'], head)
        self.assertEqual(len(self.manifest()['snapshots']), 2)

    def test_mixed_snapshot_equal_and_unknown_times_rejected(self):
        head, _ = self.load()
        for index, timestamp in enumerate((2, None)):
            v = fixture()
            v['update_time'] = timestamp
            v['title'] = 'changed synthetic title'
            sid = self.preserve(self.export('ambiguous' + str(index), [v]))
            with self.assertRaises(Rejected):
                self.a.admit(sid, head)
        self.assertEqual(self.manifest()['head'], head)

    def test_mixed_newer_and_older_conversations_reject_whole_snapshot(self):
        a, b = fixture(), fixture(); b['id'] = 'synthetic-b'
        a['update_time'] = b['update_time'] = 10
        head, _ = self.load('mixed-baseline', [a,b])
        before = canonical(self.current())
        a['update_time'] = 20; a['title'] = 'newer synthetic'
        b['update_time'] = 5; b['title'] = 'older synthetic'
        sid = self.preserve(self.export('mixed-order', [a,b]))
        with self.assertRaisesRegex(Rejected, 'not_increasing'):
            self.a.admit(sid, head)
        self.assertEqual(self.manifest()['head'], head)
        self.assertEqual(canonical(self.current()), before)
        self.a.verify()

    def test_partial_is_not_published(self):
        head, _ = self.load()
        before = canonical(self.current())
        sid = self.preserve(self.export('partial', [None, 42, fixture()]))
        result = self.a.admit(sid, head)
        self.assertEqual(result['status'], 'partial')
        self.assertGreater(sum(result['attempt']['warnings'].values()), 0)
        self.assertEqual(self.manifest()['head'], head)
        self.assertEqual(canonical(self.current()), before)
        record('partial-import', result)

    def test_malformed_json_and_corrupt_zip_preserved(self):
        head, _ = self.load()
        sid = self.preserve(self.export('malformed', raw='[' + json.dumps(fixture()) + ','))
        with self.assertRaisesRegex(Rejected, 'import_failed'):
            self.a.admit(sid, head)
        invalid = self.base / 'invalid.zip'
        invalid.write_bytes(b'synthetic invalid ZIP')
        bad = self.a.preserve(invalid)
        self.assertEqual(bad['status'], 'invalid')
        self.assertEqual(digest(invalid), bad['id'])
        self.assertEqual(self.manifest()['head'], head)
        self.a.verify()  # byte preservation, even rejected inputs

    def test_checksum_and_attachment_crc_detected(self):
        p = self.base / 'crc.zip'
        with zipfile.ZipFile(p, 'x') as z:
            z.writestr('conversations.json', json.dumps([fixture()]))
            z.writestr('binary.bin', b'unique synthetic binary payload')
        data = bytearray(p.read_bytes())
        data[data.index(b'unique synthetic binary payload')] ^= 1
        # Owned synthetic damaged input, never a user export.
        damaged = self.base / 'damaged.zip'
        damaged.write_bytes(data)
        bad = self.a.preserve(damaged)
        self.assertEqual(bad['status'], 'invalid')
        good = self.preserve(self.export('good', [fixture()]))
        obj = self.a.root / 'objects' / (good + '.zip')
        os.chmod(obj, 0o600)
        with obj.open('r+b') as f:
            f.write(b'BAD')  # explicit corruption fault against disposable fixture
        with self.assertRaisesRegex(Rejected, 'checksum'):
            self.a.admit(good, 'none')
        with self.assertRaisesRegex(Rejected, 'checksum'):
            self.a.verify()
        self.assertEqual(digest(damaged), bad['id'])

    def test_interrupted_import_and_generation_publication(self):
        head, _ = self.load()
        before = canonical(self.current())
        v = fixture(); v['update_time'] = 3; v['title'] = 'new synthetic'
        sid = self.preserve(self.export('next', [v]))
        # Real abrupt controller exit after child import, and after generation consolidation.
        for phase in ('import_finished', 'before_manifest_publish'):
            script = ('import os,sys;sys.path.insert(0,"poc");'
                      'from archive_controller.controller import Archive;'
                      'a=Archive(sys.argv[1],lambda stage: os._exit(71) if stage==sys.argv[4] else None);'
                      'a.admit(sys.argv[2],sys.argv[3])')
            p = subprocess.run([sys.executable, '-c', script, str(self.a.root), sid, head, phase], cwd=REPO, capture_output=True, timeout=30)
            self.assertEqual(p.returncode, 71, p.stderr)
            self.assertEqual(self.manifest()['head'], head)
            self.assertEqual(canonical(self.current()), before)
            self.a.verify()  # reconciles durable importing -> interrupted
            self.assertEqual(self.manifest()['snapshots'][-1]['status'], 'interrupted')
        self.assertEqual(self.a.admit(sid, head)['status'], 'imported')
        self.assertEqual(len(list((self.a.root / 'objects').glob('*.zip'))), 2)

    def test_disk_full_simulated_during_preserve_and_import(self):
        head, _ = self.load()
        originals = {p.name: digest(p) for p in (self.a.root / 'objects').iterdir()}
        def fail(stage):
            if stage in ('preserve_copy', 'before_manifest_publish'):
                raise OSError(errno.ENOSPC, 'synthetic disk full')
        failing = Archive(self.a.root, fail)
        v = fixture(); v['update_time'] = 8; v['title'] = 'disk full synthetic'
        p = self.export('diskfull', [v])
        source_hash = digest(p)
        with self.assertRaises(OSError):
            failing.preserve(p)
        sid = self.preserve(p)
        with self.assertRaises(OSError):
            failing.admit(sid, head)
        self.assertEqual(self.manifest()['head'], head)
        self.assertEqual(digest(p), source_hash)
        for name, sha in originals.items():
            self.assertEqual(digest(self.a.root / 'objects' / name), sha)
        self.a.verify()

    def test_atomic_manifest_failure_does_not_publish(self):
        head, _ = self.load()
        v = fixture(); v['update_time'] = 5; v['title'] = 'later'
        sid = self.preserve(self.export('savefail', [v]))
        real_replace = os.replace
        count = 0
        def replace(src, dst):
            nonlocal count
            if Path(dst).name == 'manifest.json':
                count += 1
                if count == 2:
                    raise OSError(errno.ENOSPC, 'synthetic manifest publication failure')
            return real_replace(src, dst)
        with patch('archive_controller.controller.os.replace', replace):
            with self.assertRaises(OSError):
                self.a.admit(sid, head)
        self.assertEqual(self.manifest()['head'], head)
        self.a.verify()

    def test_backup_restore_rebuild_after_database_loss(self):
        head, _ = self.load()
        v = fixture(); v['update_time'] = 4; v['title'] = 'updated synthetic'
        self.load('updated', [v])
        expected = canonical(self.current())
        backup = self.base / 'backup'
        self.a.backup(backup)
        before = json.loads((backup / 'SHA256.json').read_text())
        destination = self.base / 'restored'
        result = restore(backup, destination)
        self.assertTrue(result['sha256_manifest_equal'])
        self.assertEqual(inventory(destination), before)
        self.assertEqual(canonical(self.current(Archive(destination))), expected)
        self.a.verify()
        # Simulate active DB loss by moving only the disposable read model, not originals.
        self.current().rename(self.base / 'lost-reader.db')
        rebuilt = self.base / 'rebuilt.db'
        self.a.rebuild(rebuilt)
        self.assertEqual(canonical(rebuilt), expected)
        self.assertEqual(app('verify', rebuilt)['exit_code'], 0)
        self.assertEqual(len(self.manifest()['snapshots']), 2)

    def test_wal_consistency_backup(self):
        self.load()
        path = self.current()
        with sqlite3.connect(path) as writer:
            self.assertEqual(writer.execute('PRAGMA journal_mode=WAL').fetchone()[0], 'wal')
            writer.execute('PRAGMA wal_autocheckpoint=0')
            writer.execute('CREATE TABLE synthetic_committed(value)')
            writer.execute('INSERT INTO synthetic_committed VALUES (17)')
            writer.commit()
            self.assertGreater(Path(str(path) + '-wal').stat().st_size, 0)
            backup = self.base / 'wal-backup'
            self.a.backup(backup)
        restored = self.base / 'wal-restored'
        restore(backup, restored)
        with sqlite3.connect(self.current(Archive(restored))) as c:
            self.assertEqual(c.execute('SELECT value FROM synthetic_committed').fetchall(), [(17,)])

    def test_incomplete_backup_and_corruption_fail_closed(self):
        self.load()
        def fail(stage):
            if stage == 'backup_copied':
                raise InterruptedError('synthetic interrupted backup')
        backup = self.base / 'backup'
        with self.assertRaises(InterruptedError):
            Archive(self.a.root, fail).backup(backup)
        self.assertFalse(backup.exists())
        incomplete = next(self.base.glob('.backup-incomplete-*'))
        with self.assertRaisesRegex(Rejected, 'incomplete'):
            restore(incomplete, self.base / 'refused')
        self.a.backup(backup)
        obj = next((backup / 'objects').iterdir())
        with obj.open('ab') as f:
            f.write(b'damaged backup fixture')
        with self.assertRaisesRegex(Rejected, 'checksum'):
            restore(backup, self.base / 'refused')
        self.assertFalse((self.base / 'refused').exists())
        self.a.verify()

    @unittest.skipUnless(shutil.which('gpg'), 'GnuPG unavailable; encrypted recovery NOT VERIFIED')
    def test_encrypted_restore_wrong_key_and_corruption(self):
        self.load()
        backup = self.base / 'backup'
        self.a.backup(backup)
        key = self.base / 'synthetic-key'
        key.write_text('synthetic-test-only-key\n')
        os.chmod(key, 0o600)
        encrypted = self.base / 'backup.gpg'
        seal(backup, encrypted, key)
        destination = self.base / 'decrypted'
        unseal(encrypted, destination, key)
        self.assertEqual(inventory(destination), json.loads((backup / 'SHA256.json').read_text()))
        wrong = self.base / 'wrong-synthetic-key'
        wrong.write_text('wrong-test-only-passphrase\n')
        with self.assertRaisesRegex(Rejected, 'decryption_failed'):
            unseal(encrypted, self.base / 'wrong', wrong)
        self.assertFalse((self.base / 'wrong').exists())
        damaged = self.base / 'damaged.gpg'
        data = bytearray(encrypted.read_bytes()); data[len(data)//2] ^= 1
        damaged.write_bytes(data)
        with self.assertRaisesRegex(Rejected, 'decryption_failed'):
            unseal(damaged, self.base / 'damaged-restore', key)
        self.assertFalse((self.base / 'damaged-restore').exists())
        self.a.verify()

    def test_interrupted_running_cli_process(self):
        head, _ = self.load()
        before = canonical(self.current())
        v = fixture(); v['update_time'] = 12; v['title'] = 'in flight synthetic'
        sid = self.preserve(self.export('in-flight', [v]))
        # Patch only the child launcher to delay after the real import subprocess starts.
        script = r"""
import os,sys,time
sys.path.insert(0,'poc')
import archive_controller.controller as c
real=c.subprocess.Popen
marker=c.Path(sys.argv[4])
def launch(*args,**kwargs):
    child=real(*args,**kwargs)
    marker.write_text(str(child.pid))
    time.sleep(30)
    return child
c.subprocess.Popen=launch
c.Archive(sys.argv[1]).admit(sys.argv[2],sys.argv[3])
"""
        marker = self.base / 'child-pid'
        process = subprocess.Popen([sys.executable, '-c', script, str(self.a.root), sid, head, str(marker)], cwd=REPO, start_new_session=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        try:
            deadline = time.monotonic()+10
            while not marker.exists() and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertTrue(marker.exists())
            os.killpg(process.pid, signal.SIGKILL)
        finally:
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGKILL)
            process.communicate(timeout=5)
        self.assertEqual(self.manifest()['head'], head)
        self.assertEqual(canonical(self.current()), before)
        self.a.verify()
        self.assertEqual(self.manifest()['snapshots'][-1]['status'], 'interrupted')
        self.assertEqual(len(list((self.a.root / 'objects').glob('*.zip'))), 2)

    def test_controller_lock_serializes_processes(self):
        self.load()
        script = ('import sys;sys.path.insert(0,"poc");'
                  'from archive_controller.controller import Archive;'
                  'Archive(sys.argv[1]).verify()')
        with self.a.locked():
            child = subprocess.Popen([sys.executable, '-c', script, str(self.a.root)], cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            try:
                with self.assertRaises(subprocess.TimeoutExpired):
                    child.communicate(timeout=0.15)
            finally:
                pass
        out, err = child.communicate(timeout=15)
        self.assertEqual(child.returncode, 0, err)

    def test_destinations_separate_and_existing_never_overwritten(self):
        self.load()
        with self.assertRaises(Rejected):
            self.a.backup(self.a.root / 'backup')
        backup = self.base / 'backup'
        self.a.backup(backup)
        with self.assertRaises(Rejected):
            self.a.backup(backup)
        with self.assertRaises(Rejected):
            restore(backup, self.a.root)

    def test_scale_1000_conversations_lifecycle(self):
        items = []
        for i in range(1000):
            v = fixture(); v['id'] = 'scale-' + str(i)
            if i < 5:
                # 400-node long chain plus root's second branch, below 5,000-node limit.
                for j in range(400):
                    key = 'long-' + str(j)
                    parent = 'b' if j == 0 else 'long-' + str(j-1)
                    child = [] if j == 399 else ['long-' + str(j+1)]
                    node = copy.deepcopy(v['mapping']['b'])
                    node.update(id=key, parent=parent, children=child)
                    node['message']['id'] = 'msg-' + key
                    node['message']['content']['parts'] = ['synthetic long message ' + ('0123456789abcdef' * 64)]
                    v['mapping'][key] = node
                v['mapping']['b']['children'] = ['long-0']
                v['current_node'] = 'long-399'
            if i % 10 == 0:
                v['mapping']['a']['message']['content'] = {'content_type': 'multimodal_text', 'parts': ['caption', {'content_type': 'image_asset_pointer', 'asset_pointer': 'synthetic://attachment'}]}
            items.append(v)
        rng = random.Random(42)
        attachments = {'attachments/' + str(i) + '.bin': rng.randbytes(n) for i, n in enumerate((4*1024**2, 2*1024**2, 1024**2, 4096))}
        source = self.export('scale', items, attachments=attachments)
        measure = dict(conversations=1000, expected_nodes=5000, attachments=4,
                       attachment_bytes=sum(map(len, attachments.values())), zip_bytes=source.stat().st_size)
        started = time.monotonic(); sid = self.preserve(source)
        measure['preserve_seconds'] = time.monotonic() - started
        started = time.monotonic(); first = self.a.admit(sid, 'none')
        measure['initial_controller_import_seconds'] = time.monotonic()-started
        self.assertEqual(first['status'], 'imported', first)
        measure['initial_import'] = first['attempt']
        with sqlite3.connect(self.current()) as c:
            self.assertEqual(c.execute('SELECT count(*) FROM conversations').fetchone()[0], 1000)
            self.assertEqual(c.execute('SELECT count(*) FROM conversation_nodes').fetchone()[0], 5000)
            measure['sqlite_bytes'] = self.current().stat().st_size
            measure['page_size'] = c.execute('PRAGMA page_size').fetchone()[0]
            # dbstat is optional. Record exact page allocation if available.
            try:
                measure['index_table_bytes'] = dict(c.execute("SELECT name,sum(pgsize) FROM dbstat WHERE name LIKE '%fts%' GROUP BY name"))
            except sqlite3.Error:
                measure['index_table_bytes'] = None
        start = time.monotonic(); duplicate = self.a.preserve(source)
        self.assertTrue(duplicate['duplicate'])
        measure['duplicate_submission_seconds'] = time.monotonic()-start
        # Re-import identical canonical state in a different ZIP, then updated state.
        repeat = self.export('repeat', items, attachments={'new-metadata.bin': b'new synthetic member'})
        repeat_id = self.preserve(repeat)
        started = time.monotonic(); result = self.a.admit(repeat_id, sid)
        measure['controller_reimport_seconds'] = time.monotonic()-started
        self.assertEqual(result['status'], 'imported', result)
        measure['reimport'] = result['attempt']
        for v in items[:100]:
            v['update_time'] = 20
            v['title'] = 'updated synthetic'
        updated_id = self.preserve(self.export('scale-updated', items))
        started = time.monotonic(); result = self.a.admit(updated_id, repeat_id)
        measure['controller_updated_import_seconds'] = time.monotonic()-started
        self.assertEqual(result['status'], 'imported', result)
        measure['updated_import'] = result['attempt']
        measure['total_snapshot_bytes'] = sum(s['bytes'] for s in self.manifest()['snapshots'])
        measure['active_logical_bytes_before_backup'] = treebytes(self.a.root)
        measure['retained_work_bytes_before_backup'] = treebytes(self.a.root / 'work')
        measure['temporary_disk_peak_bytes'] = None
        measure['temporary_disk_note'] = 'Endpoint retained work size measured; transient peak not sampled.'
        expected = canonical(self.current())
        backup = self.base / 'scale-backup'
        started = time.monotonic(); self.a.backup(backup)
        measure['backup_seconds'] = time.monotonic()-started
        measure['backup_bytes'] = treebytes(backup)
        measure['backup_sha256_inventory'] = json.loads((backup / 'SHA256.json').read_text())
        dest = self.base / 'scale-restored'
        started = time.monotonic(); restore(backup, dest)
        measure['restore_seconds'] = time.monotonic()-started
        self.assertEqual(canonical(self.current(Archive(dest))), expected)
        self.assertEqual(inventory(dest), json.loads((backup / 'SHA256.json').read_text()))
        measure['restore_hash_inventory_equal'] = True
        rebuilt = self.base / 'scale-rebuilt.db'
        started = time.monotonic(); self.a.rebuild(rebuilt)
        measure['rebuild_seconds'] = time.monotonic()-started
        self.assertEqual(canonical(rebuilt), expected)
        measure['rebuilt_canonical_equal'] = True
        measure['max_child_rss_kib'] = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        measure['harness_max_rss_kib'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        measure['rss_note'] = 'Linux per-process high-water maxima, not summed tree RSS. Run scale alone in fresh subprocess for interpretation.'
        if shutil.which('gpg'):
            key = self.base / 'synthetic-key'
            key.write_text('synthetic-test-only-not-a-real-credential\n')
            os.chmod(key, 0o600)
            encrypted = self.base / 'scale-backup.gpg'
            started = time.monotonic(); seal(backup, encrypted, key)
            measure['encrypted_backup_seconds'] = time.monotonic()-started
            measure['encrypted_backup_bytes'] = encrypted.stat().st_size
            decrypted = self.base / 'scale-decrypted'
            started = time.monotonic(); unseal(encrypted, decrypted, key)
            measure['decrypt_restore_seconds'] = time.monotonic()-started
            self.assertEqual(inventory(decrypted), json.loads((backup / 'SHA256.json').read_text()))
            self.assertEqual(canonical(self.current(Archive(decrypted))), expected)
            measure['encrypted_backup'] = 'VERIFIED GnuPG AES256 encryption/decryption and restored hashes'
        else:
            measure['encrypted_backup'] = 'NOT VERIFIED; GnuPG unavailable'
        measure['max_child_rss_kib'] = resource.getrusage(resource.RUSAGE_CHILDREN).ru_maxrss
        measure['harness_max_rss_kib'] = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        record('scale', measure)


if __name__ == '__main__':
    unittest.main(verbosity=2)
