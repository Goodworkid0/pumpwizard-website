"""Bounded, offline research packages. Never opens a live writer or executes bundled code."""
import copy
import base64
import hashlib
import gzip
import heapq
import json
from pathlib import Path
import sqlite3
import tempfile
import time
from types import SimpleNamespace
import zlib

from .engine import canonical
from .ladder import LadderRunner
from .research_runner import ResearchRunner
from .versioned_trials import TrialLadderRunner, TrialResearchRunner
from .forward_trials_v3 import V3LadderRunner, V3ResearchRunner

SCHEMA = 'pumpwizard.replay-package/v1'
RUNNERS = (LadderRunner, ResearchRunner, TrialLadderRunner, TrialResearchRunner,
           V3LadderRunner, V3ResearchRunner)
TABLES = ('events', 'sales', 'trades', 'equity')
SOURCE_FILES = ('engine.py', 'ladder.py', 'model.py', 'research_runner.py',
                'shared_research.py', 'experiment_catalog.py', 'versioned_trials.py',
                'forward_trials_v3.py', 'replay_package.py')
MAX_BYTES = 64 * 1024 * 1024
MAX_PACKAGE_BYTES = 1024 * 1024 * 1024


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def file_digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        while block := stream.read(1024 * 1024): h.update(block)
    return h.hexdigest()


def jsonl_digest(path, limit):
    """Compare content rather than gzip encoder/version-specific byte layout."""
    h = hashlib.sha256(); total = 0
    with gzip.open(path, 'rb') as stream:
        while block := stream.read(1024 * 1024):
            total += len(block)
            if total > limit:
                raise ValueError('Oversized derived evidence')
            h.update(block)
    return h.hexdigest()


def decode_input(row):
    compressed = base64.b64decode(row['payload_zlib_base64'], validate=True)
    decoder = zlib.decompressobj()
    raw = decoder.decompress(compressed, MAX_BYTES + 1)
    if len(raw) > MAX_BYTES or not decoder.eof or decoder.unused_data:
        raise ValueError('Invalid or oversized compressed input')
    if digest(raw) != row['id']:
        raise ValueError('Archived input hash mismatch')
    batch = json.loads(raw)
    if batch['at'] != row['at']:
        raise ValueError('Archived input timestamp mismatch')
    return batch


def frozen(path):
    path = Path(path)
    if not path.is_file() or path.is_symlink():
        raise ValueError('Expected a regular frozen SQLite file')
    for suffix in ('-wal', '-journal'):
        sidecar = Path(str(path) + suffix)
        if sidecar.exists() and sidecar.stat().st_size:
            raise ValueError('Use a checkpointed backup, not a live WAL database')
    con = sqlite3.connect(path.resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
    con.row_factory = sqlite3.Row
    con.execute('PRAGMA query_only=ON')
    con.execute('PRAGMA trusted_schema=OFF')
    deadline = time.monotonic() + 180
    con.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
    return con


def runner_type(contract):
    for cls in RUNNERS:
        files = ('ladder.py', 'model.py', *getattr(cls, 'extra_code_files', ()))
        code = b''.join((Path(__file__).parent / name).read_bytes() for name in files)
        if digest(code) == contract.get('code_hash'):
            return cls
    raise ValueError('Recorded implementation is not installed; use its matching source release')


def table_proof(con, table):
    h = hashlib.sha256(); count = 0
    for row in con.execute('SELECT * FROM ' + table + ' ORDER BY id'):
        h.update((canonical(dict(row)) + '\n').encode()); count += 1
    return {'rows': count, 'sha256': h.hexdigest()}


class Output:
    def __init__(self, folder, limit=MAX_BYTES):
        self.folder, self.limit, self.used = Path(folder), limit, 0
        self.streams = {}

    def write(self, name, value, append=False):
        raw = (canonical(value) + '\n').encode()
        self.used += len(raw)
        if self.used > self.limit * 8:
            raise ValueError('Package exceeds size budget; no complete manifest was published')
        if append:
            if name not in self.streams:
                stream = (self.folder / (name + '.gz')).open('xb')
                self.streams[name] = (stream, gzip.GzipFile(filename='',mode='wb',fileobj=stream,mtime=0,compresslevel=3))
            self.streams[name][1].write(raw)
        else:
            if sum(p.stat().st_size for p in self.folder.iterdir()) + len(raw) > self.limit:
                raise ValueError('Package exceeds size budget; no complete manifest was published')
            with (self.folder / name).open('xb') as stream: stream.write(raw)
        if sum(p.stat().st_size for p in self.folder.iterdir()) > self.limit:
            raise ValueError('Package exceeds size budget; no complete manifest was published')

    def close(self, check=True):
        for stream, encoded in self.streams.values():
            encoded.close(); stream.close()
        self.streams.clear()
        if check and sum(p.stat().st_size for p in self.folder.iterdir()) > self.limit:
            raise ValueError('Package exceeds size budget; no complete manifest was published')


def read_json(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_BYTES:
        raise ValueError('Invalid package file')
    return json.loads(path.read_text())


def replay(folder, output=None, max_inputs=10000, max_seconds=180):
    """Installed implementation only; temporary simulation, no provider calls."""
    folder = Path(folder)
    contract = read_json(folder / 'contract.json')
    expected = read_json(folder / 'expected.json')
    cls = runner_type(contract)
    shared = SimpleNamespace(features={}, last_input_id=None, last_at=None)
    pending, decisions, labels = [], 0, 0
    deadline = time.monotonic() + max_seconds
    with tempfile.TemporaryDirectory(prefix='pumpwizard-replay-') as temp:
        args = [Path(temp) / 'replay.sqlite', contract['config'], contract['ownership_config']]
        if issubclass(cls, ResearchRunner):
            args.append(shared)
        kwargs = {'source': contract['source']}
        if 'experiment' in contract:
            kwargs['definition'] = contract['experiment']
        runner = cls(*args, **kwargs)
        try:
            if runner.contract != contract:
                raise ValueError('Replay contract differs from the recorded contract')
            original_reason = runner.entry_reason
            def reason(mint, market, evidence, at):
                nonlocal decisions
                phase = 'pending_recheck' if mint in runner.state['pending'] else 'candidate_screen'
                result = original_reason(mint, market, evidence, at)
                decisions += 1
                ident = 'decision-' + str(decisions)
                if output:
                    output.write('decisions.jsonl', {
                        'id': ident, 'at': at, 'mint': mint, 'phase': phase,
                        'provenance': 'replay_derived_entry_screen',
                        'input_id': shared.last_input_id, 'signal_passed': result is None,
                        'reason': result,
                        'feature_window_seconds': runner.cfg['observation_seconds'],
                        'quote_observed_at': market['observed_at'] if market else None,
                        'supported_fresh_market': market is not None}, True)
                    if market:
                        for horizon in (300, 900, 3600):
                            heapq.heappush(pending, (at + horizon, ident, horizon, mint,
                                                    market['pool'], market['route'], market['price']))
                return result
            runner.entry_reason = reason
            previous = None; count = 0
            with gzip.open(folder / 'inputs.jsonl.gz', 'rt') as stream:
                while line := stream.readline(MAX_BYTES + 1):
                    if len(line) > MAX_BYTES:
                        raise ValueError('Oversized input record')
                    if time.monotonic() > deadline or count >= max_inputs:
                        raise ValueError('Replay exceeds its time or input-count budget')
                    row = json.loads(line); saved = decode_input(row); at = saved['at']
                    if digest(canonical(saved).encode()) != row['id'] or (previous is not None and at <= previous):
                        raise ValueError('Input identity or chronology mismatch')
                    previous = at; count += 1
                    shared.features = {(f['mint'], f['window']): {k:v for k,v in f.items() if k not in ('mint','window')}
                                       for f in saved.get('features', [])}
                    shared.last_input_id = row['id']; shared.last_at = at
                    if output:
                        markets = {q['mint']: runner.market(q, at) for q in saved['quotes']}
                        retry = []
                        while pending and pending[0][0] <= at:
                            due, ident, horizon, mint, pool, route, price = item = heapq.heappop(pending)
                            m = markets.get(mint)
                            valid = (m and m['pool'] == pool and m['route'] == route and
                                     due <= m['observed_at'] <= due + 120)
                            if not valid and at <= due + 120:
                                retry.append(item); continue
                            output.write('outcomes.jsonl', {'decision_id': ident, 'horizon_seconds': horizon,
                                'due_at': due, 'observed_at': m['observed_at'] if valid else None,
                                'quality': 'observed_within_120s' if valid else 'unavailable',
                                'price_return': m['price'] / price - 1 if valid else None}, True)
                            labels += 1
                        for item in retry: heapq.heappush(pending, item)
                    batch = copy.deepcopy(saved)
                    if issubclass(cls, ResearchRunner): batch.pop('features', None)
                    runner.step(batch)
            if runner.state != expected['state']:
                raise ValueError('Final state mismatch: incomplete inputs, funding changes or implementation drift')
            for table in TABLES:
                if table_proof(runner.db, table) != expected['tables'][table]:
                    raise ValueError('Recorded ' + table + ' do not match replay')
            if output:
                for table in ('events', 'sales', 'trades'):
                    for row in runner.db.execute('SELECT * FROM ' + table + ' ORDER BY id'):
                        data = dict(row); data['data'] = json.loads(data['data'])
                        output.write(table + '.jsonl', data, True)
                for due, ident, horizon, *_ in pending:
                    output.write('outcomes.jsonl', {'decision_id': ident, 'horizon_seconds': horizon,
                        'due_at': due, 'observed_at': None, 'price_return': None,
                        'quality': 'not_observed_in_package'}, True)
                    labels += 1
            return {'status': 'verified', 'inputs': count, 'entry_screens': decisions,
                    'outcome_labels': labels, 'tables': expected['tables']}
        finally:
            runner.close()


def create(ledger, shared_archive, destination, *, frozen_source=False, max_inputs=10000, max_bytes=MAX_BYTES, max_seconds=180):
    if not frozen_source:
        raise ValueError('A frozen backup and explicit frozen_source assertion are required')
    if not 1024 <= max_bytes <= MAX_PACKAGE_BYTES or not 1 <= max_inputs <= 50000 or not 1 <= max_seconds <= 600:
        raise ValueError('Invalid package resource limits')
    folder = Path(destination)
    folder.mkdir(mode=0o700, parents=False, exist_ok=False)
    output = Output(folder, max_bytes)
    con = frozen(ledger); shared = None
    try:
        contract = json.loads(con.execute("SELECT value FROM meta WHERE key='contract'").fetchone()[0])
        cls = runner_type(contract)
        tables = {r[0] for r in con.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if 'contract_history' in tables and con.execute('SELECT count(*) FROM contract_history').fetchone()[0] > 1:
            raise ValueError('Multiple implementation eras need a segmented replay; refusing to reinterpret history')
        state = json.loads(con.execute("SELECT value FROM meta WHERE key='state'").fetchone()[0])
        output.write('contract.json', contract)
        output.write('expected.json', {'state': state, 'tables': {t: table_proof(con, t) for t in TABLES}})
        if issubclass(cls, ResearchRunner):
            if not shared_archive: raise ValueError('Shared research archive is required')
            shared = frozen(shared_archive)
            refs = con.execute('SELECT id,at FROM input_refs ORDER BY at')
        else:
            refs = con.execute('SELECT id,at FROM inputs ORDER BY at')
        count = 0
        for ref in refs:
            count += 1
            if count > max_inputs: raise ValueError('Input count exceeds the bounded package limit')
            row = (shared or con).execute('SELECT payload FROM inputs WHERE id=?', (ref['id'],)).fetchone()
            if row is None: raise ValueError('Referenced input is missing from shared archive')
            item = {'id':ref['id'], 'at':ref['at'],
                    'payload_zlib_base64':base64.b64encode(row[0]).decode('ascii')}
            decode_input(item)
            output.write('inputs.jsonl', item, True)
        if not count: raise ValueError('No input history to replay')
        output.close()
        # Source is reference material. Verification never imports code from the package.
        sources = {name: (Path(__file__).parent / name).read_text() for name in SOURCE_FILES}
        output.write('implementation.json', sources)
        proof = replay(folder, output, max_inputs, max_seconds)
        output.close()
        output.write('replay-result.json', proof)
        files = {p.name: {'bytes': p.stat().st_size, 'sha256': file_digest(p)} for p in folder.iterdir()}
        output.write('manifest.json', {'schema': SCHEMA, 'files': files, 'replay': proof,
            'size_budget_bytes': max_bytes,
            'input_limit': max_inputs, 'replay_seconds_limit': max_seconds,
            'input_encoding': 'Gzip JSONL. Each inputs row contains the original zlib payload as base64. Its id is SHA-256 of the decompressed canonical JSON.',
            'scope': 'Complete recorded input sequence for one supported instance; no live execution.',
            'decision_scope': 'Replay-derived calls to entry_reason only; capacity/attempt exclusions may bypass this screen.',
            'feature_join': 'Join decision.input_id to inputs.id; find features by decision.mint and feature_window_seconds. Evidence and raw quotes use the same input/mint. They are not duplicated in every decision row.',
            'privacy': 'Private operator artifact. Review raw public-provider fields and evidence before sharing.',
            'limitations': ['Hashes detect corruption, not authenticity from an untrusted publisher.',
                'Future outcome labels must not be used as entry-time features.',
                'Observed price labels exclude fees, liquidity impact and executable fills.',
                'Unavailable labels are not zero returns; repeated observations are not independent samples.',
                'Code-migrated or funded histories require checkpoint/segmented replay and are refused.']})
        return proof
    finally:
        output.close(check=False)
        con.close()
        if shared: shared.close()


def verify(folder):
    folder = Path(folder)
    manifest = read_json(folder / 'manifest.json')
    if manifest.get('schema') != SCHEMA: raise ValueError('Unknown package schema')
    files = manifest['files']
    allowed = {'contract.json', 'expected.json', 'inputs.jsonl.gz', 'implementation.json',
               'replay-result.json', 'decisions.jsonl.gz', 'outcomes.jsonl.gz', 'events.jsonl.gz', 'sales.jsonl.gz', 'trades.jsonl.gz'}
    if not set(files) <= allowed or not {'contract.json','expected.json','inputs.jsonl.gz','implementation.json','replay-result.json'} <= set(files):
        raise ValueError('Unexpected package inventory')
    if {p.name for p in folder.iterdir()} != set(files) | {'manifest.json'}:
        raise ValueError('Package contains unlisted files')
    limit = manifest.get('size_budget_bytes', MAX_BYTES)
    max_inputs = manifest.get('input_limit', 10000)
    max_seconds = manifest.get('replay_seconds_limit', 180)
    if type(max_inputs) is not int or not 1 <= max_inputs <= 50000 or type(max_seconds) is not int or not 1 <= max_seconds <= 600:
        raise ValueError('Invalid replay budget')
    if type(limit) is not int or not 1024 <= limit <= MAX_PACKAGE_BYTES:
        raise ValueError('Invalid package budget')
    if any(type(v['bytes']) is not int or v['bytes']<0 for v in files.values()) or sum(v['bytes'] for v in files.values()) > limit:
        raise ValueError('Oversized package')
    for name, record in files.items():
        path = folder / name
        if path.is_symlink() or not path.is_file() or path.stat().st_size != record['bytes'] or file_digest(path) != record['sha256']:
            raise ValueError('Package integrity check failed')
    sources = read_json(folder / 'implementation.json')
    if set(sources) != set(SOURCE_FILES) or any(sources[n] != (Path(__file__).parent/n).read_text() for n in SOURCE_FILES):
        raise ValueError('Install the matching source release; bundled source is never executed')
    with tempfile.TemporaryDirectory(prefix='pumpwizard-verify-') as temp:
        output = Output(temp, limit)
        try:
            proof = replay(folder, output, max_inputs, max_seconds)
            output.close()
        finally: output.close(check=False)
        if proof != manifest['replay'] or read_json(folder/'replay-result.json') != proof:
            raise ValueError('Replay proof differs from the manifest')
        for name in ('decisions.jsonl.gz', 'outcomes.jsonl.gz', 'events.jsonl.gz', 'sales.jsonl.gz', 'trades.jsonl.gz'):
            generated = Path(temp)/name
            if generated.exists() != (name in files):
                raise ValueError('Derived evidence inventory mismatch')
            if generated.exists() and jsonl_digest(generated, limit*8) != jsonl_digest(folder/name, limit*8):
                raise ValueError('Derived evidence does not match replay')
        return proof
