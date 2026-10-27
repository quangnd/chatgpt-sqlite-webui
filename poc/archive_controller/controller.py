"""Linux, trusted local directories, one controller owner; standard library only."""
import argparse
from contextlib import contextmanager, closing
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import uuid
import zipfile

REPO = Path(__file__).resolve().parents[2]
APP = REPO / 'chatgpt_archive.py'


class Rejected(RuntimeError):
    pass


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def syncdir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def durable(path):
    with Path(path).open('rb') as f:
        os.fsync(f.fileno())


def write_json(path, value):
    temp = path.with_name('.' + path.name + '-' + uuid.uuid4().hex)
    with temp.open('x') as f:
        json.dump(value, f, indent=2, sort_keys=True, allow_nan=False)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(temp, path)
    syncdir(path.parent)


def dbcopy(source, target):
    with closing(sqlite3.connect(f'{Path(source).resolve().as_uri()}?mode=ro', uri=True)) as src:
        with closing(sqlite3.connect(target)) as dst:
            src.backup(dst)
            dst.execute('PRAGMA journal_mode=DELETE')
    durable(target)


def app(command, db, source=None, timeout=180):
    args = [sys.executable, str(APP), command, '--db', str(db), '--log-level', 'none']
    if source is not None:
        args += ['--input', str(source), '--rebuild-fts']
    start = time.monotonic()
    p = subprocess.Popen(args, cwd=REPO, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        out, err = p.communicate(timeout=timeout)
    except BaseException:
        p.terminate()
        try:
            p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            p.kill()
            p.communicate(timeout=5)
        raise
    # Never retain raw CLI diagnostics: they can contain source paths or content.
    codes = {}
    safe_codes = {b'summary_update_after_commit_failed', b'import_connection_close_failed',
                  b'summary_update_after_close_failed', b'cleanup_warning'}
    for line in (out + b'\n' + err).splitlines():
        token = line.split(maxsplit=1)[:1]
        if token and token[0] in safe_codes:
            code = token[0].decode('ascii')
            codes[code] = codes.get(code, 0) + 1
    return dict(exit_code=p.returncode, seconds=time.monotonic()-start,
                warnings=codes, stderr_bytes=len(err), diagnostic_bytes=len(out)+len(err), diagnostic_sha256=hashlib.sha256(out+err).hexdigest())


def checkdb(path):
    with closing(sqlite3.connect(f'{Path(path).resolve().as_uri()}?mode=ro', uri=True)) as c:
        if c.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise Rejected('sqlite_integrity_failed')
        if c.execute('PRAGMA foreign_key_check').fetchone():
            raise Rejected('sqlite_foreign_keys_failed')
    result = app('verify', path)
    if result['exit_code']:
        raise Rejected('application_verify_failed')
    return result


def inventory(root):
    result = {}
    for p in sorted(root.rglob('*')):
        if p.is_symlink():
            raise Rejected('backup_symlink')
        if p.is_file() and p != root / 'SHA256.json':
            result[p.relative_to(root).as_posix()] = digest(p)
    return result


def outside(a, b):
    a, b = Path(a).resolve(), Path(b).resolve()
    if a == b or a in b.parents or b in a.parents:
        raise Rejected('directories_must_be_independent')


class Archive:
    def __init__(self, root, fault=None):
        self.root = Path(root).resolve()
        self.fault = fault or (lambda stage: None)

    @contextmanager
    def locked(self):
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.root / '.lock').open('a') as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            for name in ('objects', 'databases', 'work'):
                (self.root / name).mkdir(exist_ok=True, mode=0o700)
            manifest = self.root / 'manifest.json'
            m = json.loads(manifest.read_text()) if manifest.exists() else dict(version=1, head=None, database=None, snapshots=[], next_order=1)
            if m['version'] != 1:
                raise Rejected('manifest_version')
            changed = False
            for s in m['snapshots']:
                if s['status'] == 'importing':
                    s['status'] = 'interrupted'
                    s['attempts'][-1]['outcome'] = 'interrupted'
                    changed = True
            if changed:
                self.save(m)
            yield m

    def save(self, m):
        write_json(self.root / 'manifest.json', m)

    def object(self, s):
        return self.root / 'objects' / (s['sha256'] + '.zip')

    def current(self, m):
        return self.root / m['database'] if m['database'] else None

    def validate(self, s):
        p = self.object(s)
        if p.stat().st_size != s['bytes'] or digest(p) != s['sha256']:
            raise Rejected('snapshot_checksum_mismatch')
        # Stream every member, including attachments; do not extract paths.
        with zipfile.ZipFile(p) as z:
            infos = z.infolist()
            if len(infos) > 100000 or sum(i.file_size for i in infos) > 512 * 1024**2:
                raise Rejected('poc_zip_resource_limit')
            if len({i.filename for i in infos}) != len(infos):
                raise Rejected('duplicate_zip_member')
            for info in infos:
                with z.open(info) as f:
                    while f.read(1024 * 1024):
                        pass

    def preserve(self, source):
        source = Path(source)
        with self.locked() as m:
            temp = self.root / 'work' / (uuid.uuid4().hex + '.part')
            h = hashlib.sha256()
            size = 0
            with source.open('rb') as src, temp.open('xb') as dst:
                for block in iter(lambda: src.read(1024 * 1024), b''):
                    dst.write(block)
                    h.update(block)
                    size += len(block)
                    self.fault('preserve_copy')
                dst.flush()
                os.fsync(dst.fileno())
            sha = h.hexdigest()
            for s in m['snapshots']:
                if s['id'] == sha:
                    # Never heal/overwrite an existing damaged object silently.
                    if digest(self.object(s)) != sha:
                        raise Rejected('existing_snapshot_corrupt')
                    temp.unlink()  # owned disposable copy; canonical original is untouched
                    return dict(id=sha, duplicate=True, status=s['status'])
            target = self.root / 'objects' / (sha + '.zip')
            os.chmod(temp, 0o400)
            try:
                os.link(temp, target)  # exclusive publication, never replace an original
            except FileExistsError:
                if digest(target) != sha:
                    raise Rejected('existing_object_corrupt')
            syncdir(target.parent)
            temp.unlink()  # canonical object owns the preserved bytes
            syncdir(temp.parent)
            s = dict(id=sha, sha256=sha, bytes=size,
                     acquired_at=datetime.now(timezone.utc).isoformat(),
                     status='preserved', import_order=None, attempts=[])
            m['snapshots'].append(s)
            self.save(m)  # preserve even malformed exports for diagnosis
            try:
                self.validate(s)
            except (Rejected, OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
                s['status'] = 'invalid'
                s['validation_error'] = str(exc) if isinstance(exc, Rejected) else type(exc).__name__
            else:
                s['status'] = 'ready'
            self.save(m)
            return dict(id=sha, duplicate=False, status=s['status'])

    def details(self, db):
        with closing(sqlite3.connect(db)) as c:
            run = c.execute('SELECT id,status,summary_json FROM import_runs ORDER BY id DESC LIMIT 1').fetchone()
            summary = json.loads(run[2] or '{}')
            warnings = dict(c.execute('SELECT warning_type,count(*) FROM import_warnings WHERE import_run_id=? GROUP BY warning_type', (run[0],)))
            # Only numeric counts from app summary. No content or source identifiers.
            counts = {k: v for k, v in summary.items() if isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)}
            return dict(run_status=run[1], counts=counts, warnings=warnings)

    def guard(self, old, new):
        if old is None:
            return
        with closing(sqlite3.connect(old)) as a, closing(sqlite3.connect(new)) as b:
            fields = 'update_time,aggregate_hash,title,create_time,current_node,is_archived,is_starred,default_model_slug,metadata_json,exported_id'
            for oldrow in a.execute('SELECT conversation_id,' + fields + ' FROM conversations'):
                cid, ts = oldrow[:2]
                row = b.execute('SELECT ' + fields + ' FROM conversations WHERE conversation_id=?', (cid,)).fetchone()
                if row is None:
                    raise Rejected('conversation_disappeared')
                if tuple(oldrow[1:]) == row:
                    continue
                if not all(isinstance(t, (int, float)) and math.isfinite(t) for t in (ts, row[0])):
                    raise Rejected('changed_conversation_time_unknown')
                if row[0] <= ts:
                    raise Rejected('conversation_time_not_increasing')

    def admit(self, snapshot_id, after):
        with self.locked() as m:
            s = next((s for s in m['snapshots'] if s['id'] == snapshot_id), None)
            if s is None:
                raise Rejected('unknown_snapshot')
            if s['import_order'] is not None:
                self.validate(s)
                return dict(id=snapshot_id, duplicate=True, status=s['status'])
            if after != (m['head'] or 'none'):
                raise Rejected('explicit_predecessor_required')
            if m['database'] and not self.current(m).is_file():
                raise Rejected('active_database_missing_use_rebuild')
            try:
                self.validate(s)
            except (Rejected, OSError, zipfile.BadZipFile, RuntimeError, NotImplementedError) as exc:
                s['status'] = 'invalid'
                s['validation_error'] = str(exc) if isinstance(exc, Rejected) else type(exc).__name__
                self.save(m)
                raise
            attempt = dict(order=m['next_order'], outcome='importing',
                           application_sha256=digest(APP), python=sys.version.split()[0], sqlite=sqlite3.sqlite_version)
            m['next_order'] += 1
            s['attempts'].append(attempt)
            s['status'] = 'importing'
            self.save(m)
            stage = self.root / 'work' / (uuid.uuid4().hex + '.db')
            try:
                self.fault('import_started')
                if self.current(m):
                    dbcopy(self.current(m), stage)
                attempt['cli'] = app('import', stage, self.object(s))
                self.fault('import_finished')
                if stage.exists():
                    attempt.update(self.details(stage))
                if attempt['cli']['exit_code'] != 0 or attempt.get('run_status') != 'finished':
                    raise Rejected('import_failed')
                if (attempt['warnings'] or attempt['counts'].get('skipped_invalid_elements', 0)
                        or attempt['counts'].get('warnings', 0) or attempt['cli']['warnings']
                        or attempt['cli']['stderr_bytes']):
                    s['status'] = 'partial'
                    attempt['outcome'] = 'partial'
                    self.save(m)
                    return dict(id=snapshot_id, status='partial', attempt=attempt)
                attempt['verify'] = checkdb(stage)
                self.guard(self.current(m), stage)
                # Consolidate WAL using online backup; immutable generation published first.
                gen = 'databases/' + uuid.uuid4().hex + '.db'
                dbcopy(stage, self.root / gen)
                syncdir(self.root / 'databases')
                self.fault('before_manifest_publish')
                s['status'] = 'imported'
                s['import_order'] = attempt['order']
                attempt['outcome'] = 'imported'
                m['head'], m['database'] = snapshot_id, gen
                self.save(m)  # sole atomic commit point: readers resolve this pointer
                return dict(id=snapshot_id, status='imported', attempt=attempt)
            except BaseException as exc:
                # If final save failed, leave disk's old pointer authoritative.
                s['status'] = 'failed'
                attempt['outcome'] = str(exc) if isinstance(exc, Rejected) else type(exc).__name__
                # Do not persist the modified head/generation after a failed publication.
                disk = json.loads((self.root / 'manifest.json').read_text())
                if disk['head'] != snapshot_id:
                    disk_s = next(x for x in disk['snapshots'] if x['id'] == snapshot_id)
                    disk_s['status'], disk_s['attempts'] = s['status'], s['attempts']
                    self.save(disk)
                raise

    def verify(self):
        with self.locked() as m:
            for s in m['snapshots']:
                if digest(self.object(s)) != s['sha256'] or self.object(s).stat().st_size != s['bytes']:
                    raise Rejected('snapshot_checksum_mismatch')
            return dict(snapshots=len(m['snapshots']), database=checkdb(self.current(m)) if m['database'] else None)

    def backup(self, destination):
        destination = Path(destination).resolve()
        outside(self.root, destination)
        if destination.exists():
            raise Rejected('destination_exists')
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = destination.with_name('.' + destination.name + '-incomplete-' + uuid.uuid4().hex)
        with self.locked() as m:
            if not m['database']:
                raise Rejected('no_database')
            stage.mkdir(mode=0o700)
            (stage / 'objects').mkdir()
            (stage / Path(m['database']).parent).mkdir()
            for s in m['snapshots']:
                source = self.object(s)
                if digest(source) != s['sha256'] or source.stat().st_size != s['bytes']:
                    raise Rejected('snapshot_checksum_mismatch')
                shutil.copyfile(source, stage / 'objects' / source.name)
                durable(stage / 'objects' / source.name)
            write_json(stage / 'manifest.json', m)
            dbcopy(self.current(m), stage / m['database'])
            checkdb(stage / m['database'])
            self.fault('backup_copied')
            write_json(stage / 'COMPLETE.json', dict(format=1, encryption='NONE', head=m['head']))
            write_json(stage / 'SHA256.json', inventory(stage))
            for directory in (stage / 'objects', stage / 'databases', stage):
                syncdir(directory)
            self.fault('backup_before_publish')
            os.rename(stage, destination)
            syncdir(destination.parent)
        return dict(encryption='NOT VERIFIED', sha256=digest(destination / 'SHA256.json'))

    def rebuild(self, destination):
        destination = Path(destination).resolve()
        outside(self.root, destination)
        if destination.exists():
            raise Rejected('destination_exists')
        destination.parent.mkdir(parents=True, exist_ok=True)
        stage = destination.with_name('.' + destination.name + '-' + uuid.uuid4().hex)
        with self.locked() as m:
            admitted = sorted((s for s in m['snapshots'] if s['import_order'] is not None), key=lambda s: s['import_order'])
            if not admitted:
                raise Rejected('no_admitted_snapshots')
            for s in admitted:
                self.validate(s)
                result = app('import', stage, self.object(s))
                detail = self.details(stage)
                if (result['exit_code'] or detail['run_status'] != 'finished' or detail['warnings']
                        or detail['counts'].get('skipped_invalid_elements', 0)
                        or result['warnings'] or result['stderr_bytes']):
                    raise Rejected('replay_not_complete')
            checkdb(stage)
            final = destination.with_name('.' + destination.name + '-consistent-' + uuid.uuid4().hex)
            dbcopy(stage, final)
            os.link(final, destination)  # exclusive atomic publication of a consistent read DB
            syncdir(destination.parent)
            return dict(replayed=len(admitted), database_sha256=digest(destination))


def restore(source, destination):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    outside(source, destination)
    if destination.exists():
        raise Rejected('destination_exists')
    if not (source / 'COMPLETE.json').is_file() or not (source / 'SHA256.json').is_file():
        raise Rejected('incomplete_backup')
    hashes = json.loads((source / 'SHA256.json').read_text())
    if inventory(source) != hashes:
        raise Rejected('backup_checksum_mismatch')
    destination.parent.mkdir(parents=True, exist_ok=True)
    stage = destination.with_name('.' + destination.name + '-incomplete-' + uuid.uuid4().hex)
    shutil.copytree(source, stage)
    if inventory(stage) != hashes:
        raise Rejected('restored_checksum_mismatch')
    m = json.loads((stage / 'manifest.json').read_text())
    if m['version'] != 1 or not m['database'] or Path(m['database']).is_absolute() or '..' in Path(m['database']).parts:
        raise Rejected('invalid_backup_manifest')
    for s in m['snapshots']:
        if len(s['sha256']) != 64 or any(ch not in '0123456789abcdef' for ch in s['sha256']):
            raise Rejected('invalid_snapshot_hash')
        p = stage / 'objects' / (s['sha256'] + '.zip')
        if digest(p) != s['sha256'] or p.stat().st_size != s['bytes']:
            raise Rejected('snapshot_checksum_mismatch')
        os.chmod(p, 0o400)
    checkdb(stage / m['database'])
    # verify can create SQLite sidecars; hash-authoritative regular payload stays unchanged.
    if inventory(stage) != hashes:
        raise Rejected('restore_verification_changed_payload')
    for p in stage.rglob('*'):
        if p.is_file():
            durable(p)
    for p in sorted((p for p in stage.rglob('*') if p.is_dir()), reverse=True):
        syncdir(p)
    syncdir(stage)
    os.rename(stage, destination)
    syncdir(destination.parent)
    return dict(sha256_manifest_equal=True, encryption='NOT VERIFIED')


def crypt_file(source, destination, key_file, work, decrypt=False):
    """Delegate all encryption/integrity to installed GnuPG; key never in argv/logs."""
    if shutil.which('gpg') is None:
        raise Rejected('encryption_tool_unavailable')
    with tempfile.TemporaryDirectory(prefix='poc-gpg-') as private:
        home = Path(private)
        agent = subprocess.Popen(['gpg-agent', '--no-options', '--homedir', str(home),
                                  '--daemon', '--no-detach', '--disable-scdaemon'],
                                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 5
            while not (home / 'S.gpg-agent').exists():
                if agent.poll() is not None or time.monotonic() >= deadline:
                    raise Rejected('gpg_agent_unavailable')
                time.sleep(0.02)
            fd = os.open(key_file, os.O_RDONLY)
            try:
                args = ['gpg', '--no-options', '--homedir', str(home), '--batch', '--yes',
                        '--no-autostart', '--pinentry-mode', 'loopback', '--no-symkey-cache',
                        '--passphrase-fd', str(fd), '--output', str(destination)]
                args += ['--decrypt', str(source)] if decrypt else ['--symmetric', '--cipher-algo', 'AES256', str(source)]
                result = subprocess.run(args, pass_fds=(fd,), capture_output=True, timeout=180)
                if result.returncode:
                    raise Rejected('gpg_decryption_failed' if decrypt else 'gpg_encryption_failed')
            finally:
                os.close(fd)
        finally:
            agent.terminate()
            try:
                agent.wait(timeout=5)
            except subprocess.TimeoutExpired:
                agent.kill()
                agent.wait(timeout=5)


def seal(source, destination, key_file):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    outside(source, destination)
    if destination.exists():
        raise Rejected('destination_exists')
    if not (source / 'COMPLETE.json').is_file() or inventory(source) != json.loads((source / 'SHA256.json').read_text()):
        raise Rejected('incomplete_or_corrupt_backup')
    destination.parent.mkdir(parents=True, exist_ok=True)
    work = destination.parent / ('.seal-incomplete-' + uuid.uuid4().hex)
    work.mkdir(mode=0o700)
    bundle = work / 'backup.tar'
    with tarfile.open(bundle, 'x') as tar:
        for p in sorted(source.rglob('*')):
            if p.is_file():
                tar.add(p, arcname=p.relative_to(source).as_posix(), recursive=False)
    crypt_file(bundle, work / 'backup.gpg', key_file, work)
    durable(work / 'backup.gpg')
    os.link(work / 'backup.gpg', destination)
    syncdir(destination.parent)
    return dict(encryption='GnuPG AES256 symmetric', bytes=destination.stat().st_size,
                sha256=digest(destination), plaintext_staging_retained=True)


def unseal(source, destination, key_file):
    source, destination = Path(source).resolve(), Path(destination).resolve()
    outside(source, destination)
    if destination.exists():
        raise Rejected('destination_exists')
    destination.parent.mkdir(parents=True, exist_ok=True)
    work = destination.parent / ('.unseal-incomplete-' + uuid.uuid4().hex)
    work.mkdir(mode=0o700)
    bundle = work / 'backup.tar'
    crypt_file(source, bundle, key_file, work, decrypt=True)
    payload = work / 'payload'
    payload.mkdir(mode=0o700)
    total, names = 0, set()
    # No extractall, links, devices, traversal, duplicate names, or unbounded unpack.
    with tarfile.open(bundle, 'r:') as tar:
        for member in tar:
            path = Path(member.name)
            total += member.size
            if (not member.isfile() or path.is_absolute() or '..' in path.parts
                    or member.name in names or total > 1024**3 or len(names) >= 100000):
                raise Rejected('unsafe_backup_bundle')
            names.add(member.name)
            target = payload / path
            target.parent.mkdir(parents=True, exist_ok=True)
            with tar.extractfile(member) as src, target.open('xb') as dst:
                shutil.copyfileobj(src, dst, 1024*1024)
    result = restore(payload, destination)
    result['encryption'] = 'GnuPG encryption/decryption VERIFIED'
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    subs = parser.add_subparsers(dest='command', required=True)
    p = subs.add_parser('preserve'); p.add_argument('zip', type=Path)
    p = subs.add_parser('import'); p.add_argument('id'); p.add_argument('--after', required=True)
    subs.add_parser('verify'); subs.add_parser('status')
    for name in ('backup', 'rebuild'):
        p = subs.add_parser(name); p.add_argument('destination', type=Path)
    p = subs.add_parser('restore'); p.add_argument('source', type=Path)
    for name in ('seal', 'unseal'):
        p = subs.add_parser(name); p.add_argument('source', type=Path); p.add_argument('--key-file', type=Path, required=True)
        if name == 'seal': p.add_argument('destination', type=Path)
    args = parser.parse_args()
    archive = Archive(args.root)
    try:
        if args.command == 'preserve': result = archive.preserve(args.zip)
        elif args.command == 'import': result = archive.admit(args.id, args.after)
        elif args.command == 'verify': result = archive.verify()
        elif args.command == 'backup': result = archive.backup(args.destination)
        elif args.command == 'rebuild': result = archive.rebuild(args.destination)
        elif args.command == 'restore': result = restore(args.source, args.root)
        elif args.command == 'seal': result = seal(args.source, args.destination, args.key_file)
        elif args.command == 'unseal': result = unseal(args.source, args.root, args.key_file)
        else:
            with archive.locked() as m: result = m
        print(json.dumps(result, sort_keys=True, allow_nan=False))
        if result.get('status') in ('invalid', 'partial', 'failed'):
            sys.exit(2)
    except (Rejected, OSError, sqlite3.Error, zipfile.BadZipFile, tarfile.TarError, RuntimeError, ValueError, subprocess.TimeoutExpired) as exc:
        print(json.dumps(dict(error=str(exc) if isinstance(exc, Rejected) else type(exc).__name__)))
        sys.exit(2)


if __name__ == '__main__':
    main()
