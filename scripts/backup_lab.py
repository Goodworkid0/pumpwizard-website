"""Verified SQLite backup and non-destructive restore rehearsal. Never starts a writer."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time

CONFIGS = ('brain.config.json', 'community.config.json', 'dashboard.config.json',
           'ladder.config.json', 'long_hold_experiments.json', 'ownership.config.json',
           'paper.cadence.config.json', 'paper.config.json', 'paper.frequent.config.json',
           'research_experiments.json', 'simulation_sampler.config.json',
           'strategy.paper.json', 'trial_experiments.json', 'v3_trial_experiments.json')


def digest(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def sync(path):
    with path.open('rb') as stream: os.fsync(stream.fileno())


def write_manifest(destination, manifest):
    with (destination/'manifest.json').open('x') as stream:
        json.dump(manifest, stream, indent=2, allow_nan=False)
        stream.write('\n'); stream.flush(); os.fsync(stream.fileno())
    for folder in (destination/'data', destination/'config', destination):
        fd = os.open(folder, os.O_RDONLY)
        try: os.fsync(fd)
        finally: os.close(fd)


def inventory(root):
    sources = sorted((root/'data').glob('*.sqlite'))
    if not sources: raise ValueError('No SQLite ledgers found')
    configs = [root/name for name in CONFIGS if (root/name).exists()]
    if any(p.is_symlink() or not p.is_file() for p in sources+configs):
        raise ValueError('Sources must be regular files, not symlinks')
    return sources, configs


def database_check(path):
    # Only called on frozen backup files, never the live source. Immutable mode
    # avoids creating WAL/SHM sidecars during verification of an archive.
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro&immutable=1', uri=True)) as con:
        con.execute('PRAGMA query_only=ON')
        deadline = time.monotonic()+300
        con.set_progress_handler(lambda: int(time.monotonic()>deadline), 10000)
        if con.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise ValueError('SQLite integrity check failed')
        tables = [r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name")]
        return {name: con.execute('SELECT COUNT(*) FROM "'+name.replace('"', '""')+'"').fetchone()[0] for name in tables}


def prepare_destination(destination):
    if destination.exists() or destination.is_symlink():
        raise ValueError('Destination must not exist; never overwrite a backup or live directory')
    if not destination.parent.is_dir(): raise ValueError('Create the backup parent directory first')
    destination.mkdir(mode=0o700)
    (destination/'data').mkdir(mode=0o700)
    (destination/'config').mkdir(mode=0o700)


def plan(root, parent):
    sources, configs = inventory(root)
    size = sum(p.stat().st_size for p in sources+configs)
    # Include a margin for committed WAL pages and collection growth during backup.
    size += sum(p.stat().st_size for p in (root/'data').glob('*.sqlite-wal') if p.is_file())
    required = int(size*1.25)+1024**3
    return {'databases': len(sources), 'configurations': len(configs), 'estimated_source_bytes': size,
            'required_free_bytes': required, 'available_bytes': shutil.disk_usage(parent).free}


def backup(root, destination, frozen=False, timeout=300):
    root, destination = root.resolve(), destination.absolute()
    registry=root/'data/public-registry.json'
    if registry.exists() and json.loads(registry.read_text()).get('ledger_backend')=='postgres':
        raise ValueError('Authoritative ledgers are PostgreSQL; use backup_postgres_runtime, not stale SQLite files')
    if destination == root or root in destination.parents:
        raise ValueError('Keep backups outside the live project directory')
    sizing = plan(root, destination.parent)
    if sizing['available_bytes'] < sizing['required_free_bytes']: raise ValueError('Insufficient free space for backup')
    sources, configs = inventory(root)
    config_hashes = {p.name: digest(p) for p in configs}
    prepare_destination(destination)
    manifest = {'schema': 'pumpwizard.backup/v1', 'started_at': time.time(),
                'consistency': 'operator-asserted-frozen' if frozen else 'online-per-database-not-cross-ledger-atomic',
                'files': [], 'secrets_included': False}
    for path in sources+configs:
        relative = ('data/' if path in sources else 'config/')+path.name
        target = destination/relative
        started = time.time()
        if path in sources:
            deadline = time.monotonic()+timeout
            def progress(*_):
                if time.monotonic()>deadline: raise TimeoutError('SQLite backup exceeded its time budget')
            with closing(sqlite3.connect(path.as_uri()+'?mode=ro', uri=True, timeout=1)) as source:
                with closing(sqlite3.connect(target)) as copied:
                    source.backup(copied, pages=256, progress=progress, sleep=0.01)
                    copied.execute('PRAGMA journal_mode=DELETE')
            counts = database_check(target)
        else:
            shutil.copyfile(path, target); counts = None
        target.chmod(0o600); sync(target)
        manifest['files'].append({'path': relative, 'bytes': target.stat().st_size, 'sha256': digest(target),
                                  'table_counts': counts, 'started_at': started, 'completed_at': time.time()})
    if inventory(root) != (sources, configs) or config_hashes != {p.name: digest(p) for p in configs}:
        raise ValueError('Configuration or ledger inventory changed during backup; retry in a stable window')
    for item in manifest['files']:
        if item['path'].startswith('config/') and item['sha256'] != config_hashes[Path(item['path']).name]:
            raise ValueError('Configuration changed while being copied')
    manifest['completed_at'] = time.time()
    write_manifest(destination, manifest)
    return verify(destination)


def verify(directory):
    directory = directory.absolute()
    if directory.is_symlink(): raise ValueError('Backup directory must not be a symlink')
    file = directory/'manifest.json'
    if file.is_symlink() or file.stat().st_size > 5_000_000: raise ValueError('Invalid manifest')
    manifest = json.loads(file.read_text())
    if manifest.get('schema') != 'pumpwizard.backup/v1' or not manifest.get('completed_at'):
        raise ValueError('Incomplete or unsupported backup')
    seen = set()
    for item in manifest['files']:
        parts = Path(item['path']).parts
        if (len(parts) != 2 or parts[0] not in ('data', 'config') or '..' in parts
                or (parts[0] == 'data' and not parts[1].endswith('.sqlite'))
                or (parts[0] == 'config' and parts[1] not in CONFIGS) or item['path'] in seen):
            raise ValueError('Unsafe or duplicate manifest path')
        seen.add(item['path']); path = directory/item['path']
        if (path.is_symlink() or path.parent.is_symlink() or not path.is_file()
                or path.stat().st_size != item['bytes'] or digest(path) != item['sha256']):
            raise ValueError('Backup file missing or checksum mismatch: '+item['path'])
        if parts[0] == 'data' and database_check(path) != item['table_counts']:
            raise ValueError('Restored table counts differ')
    actual = {str(p.relative_to(directory)) for folder in ('data','config') for p in (directory/folder).iterdir()}
    if actual != seen or not any(p.startswith('data/') for p in seen):
        raise ValueError('Unexpected or missing backup files')
    return manifest


def restore(source, destination):
    manifest = verify(source)
    prepare_destination(destination)
    for item in manifest['files']:
        target = destination/item['path']
        shutil.copyfile(source/item['path'], target); target.chmod(0o600); sync(target)
    write_manifest(destination, manifest)
    return verify(destination)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    for action in ('plan', 'create'):
        p = sub.add_parser(action); p.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
        p.add_argument('--destination', type=Path, required=True)
        if action == 'create': p.add_argument('--frozen-source', action='store_true', help='Operator assertion that every writer is stopped')
    p = sub.add_parser('verify'); p.add_argument('--source', type=Path, required=True)
    p = sub.add_parser('restore'); p.add_argument('--source', type=Path, required=True); p.add_argument('--destination', type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.command == 'plan': result = plan(args.root, args.destination.parent)
        elif args.command == 'create': result = backup(args.root, args.destination, args.frozen_source)
        elif args.command == 'verify': result = verify(args.source)
        else: result = restore(args.source, args.destination)
        print(json.dumps(result, indent=2))
    except (OSError, ValueError, sqlite3.Error, KeyError, TypeError) as exc:
        parser.exit(1, 'Backup operation failed: '+str(exc)+'\n')


if __name__ == '__main__': main()
