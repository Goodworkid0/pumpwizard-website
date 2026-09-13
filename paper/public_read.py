"""Published public read models. HTTP traffic never builds simulator reports."""
import copy
import json
import logging
import math
import re
import sqlite3
import threading
import time
from contextlib import ExitStack, contextmanager
from pathlib import Path

ID = re.compile(r'[a-z0-9-]{1,40}\Z')


class ReadError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def encode(data):
    return json.dumps(data, allow_nan=False, separators=(',', ':')).encode()


def readonly(path, deadline=None):
    from .postgres_runtime import runtime_readonly
    runtime = runtime_readonly(path, deadline)
    if runtime is not None: return runtime
    con = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=1)
    con.row_factory = sqlite3.Row
    from .shared_inputs import read_connection
    con=read_connection(con,path)
    con.execute('PRAGMA query_only=ON')
    if deadline:
        con.set_progress_handler(lambda: int(time.monotonic() > deadline), 10000)
    return con


class ReadRows:
    def __init__(self, rows): self.rows = rows
    def fetchone(self): return self.rows[0] if self.rows else None
    def fetchall(self): return self.rows
    def __iter__(self): return iter(self.rows)


class ReadTransaction:
    """Nested report locks become short read transactions, not writer locks."""
    def __init__(self, database, runner=None, original=None):
        self.database, self.runner, self.original = database, runner, original
        self.depth = 0
    def __enter__(self):
        if self.depth == 0:
            self.database.open()
            if self.runner:
                state = self.database.execute("SELECT value FROM meta WHERE key='state'").fetchone()
                if state: self.runner.state = json.loads(state[0])
                else:
                    with self.original.lock: self.runner.state = copy.deepcopy(self.original.state)
        self.depth += 1
        return self
    def __exit__(self, *args):
        self.depth -= 1
        if self.depth == 0: self.database.close()


class ReadDatabase:
    def __init__(self, path, deadline):
        self.path, self.deadline, self.connection = path, deadline, None
    def open(self):
        if self.connection is None:
            self.connection = readonly(self.path, self.deadline)
            self.connection.execute('BEGIN')
    def close(self):
        if self.connection:
            self.connection.close(); self.connection = None
    def execute(self, sql, parameters=()):
        own = self.connection is None
        self.open()
        try: return ReadRows(self.connection.execute(sql, parameters).fetchall())
        finally:
            if own: self.close()


@contextmanager
def read_brain(brain, timeout=30):
    """Reuse reporting semantics with committed state and separate RO connections.

    Each ledger has its own read transaction, not a cross-ledger atomic snapshot.
    No original runner/memory connection or writer lock is used for queries.
    """
    deadline = time.monotonic() + timeout
    with ExitStack() as stack:
        shadow = copy.copy(brain)
        with brain.strategy_lock:
            originals = [brain.primary_strategy] + list(brain.experiment_runners)
        items = []
        for item in originals:
            original = item['runner']
            runner = copy.copy(original)
            runner.db = ReadDatabase(original.path, deadline)
            stack.callback(runner.db.close)
            runner.lock = ReadTransaction(runner.db, runner, original)
            with runner.lock: pass
            items.append(dict(item, runner=runner))
        shadow.primary_strategy = items[0]
        shadow.runner = items[0]['runner']
        shadow.experiment_runners = items[1:]
        shadow.sampler = None  # retired public sampler is not a supported active cohort
        shadow.memory = copy.copy(brain.memory)
        shadow.memory.db = ReadDatabase(brain.root / 'data/coin-memory.sqlite', deadline)
        shadow.memory.lock = ReadTransaction(shadow.memory.db)
        stack.callback(shadow.memory.db.close)
        shadow.status = dict(brain.status)
        shadow.research_timing = dict(brain.research_timing)
        yield shadow


def brief(strategy):
    report = strategy['report']
    compact = {key: value for key, value in report.items()
               if key not in ('positions', 'trades', 'events', 'candidates')}
    compact['open_position_count'] = len(report.get('positions', []))
    return {key: strategy.get(key) for key in ('id', 'label', 'trial', 'description', 'family')} | {'report': compact}


def dead(report):
    """Display classification only: current total wallet value is below $50."""
    balance = report.get('equity_usd') if isinstance(report, dict) else None
    return type(balance) in (int, float) and math.isfinite(balance) and balance < 50


def integer(query, name, default, low, high):
    try:
        value = int(query.get(name, [str(default)])[0])
        if not low <= value <= high:
            raise ValueError()
        return value
    except (ValueError, TypeError):
        raise ReadError(400, 'Invalid ' + name)


def catalog(snapshot, query):
    value = lambda key: query.get(key, [''])[0]
    q, family, state, outcome, version = [value(key) for key in ('q', 'family', 'status', 'performance', 'version')]
    if len(q) > 160 or len(family) > 80:
        raise ReadError(400, 'Search is too long')
    if state not in ('', 'active', 'observing', 'warming_up') or version not in ('', 'original', 'trials'):
        raise ReadError(400, 'Invalid catalog filter')
    if outcome not in ('', 'profitable', 'unprofitable', 'positive_24h', 'winning_record', 'losing_record', 'awaiting_record'):
        raise ReadError(400, 'Invalid performance filter')
    sort = value('sort') or 'pnl_usd'
    ascending = sort.endswith('_asc')
    metric = sort[:-4] if ascending else sort
    if metric not in ('pnl_usd', 'pnl_24h_usd', 'return_pct', 'equity_usd', 'closed_count', 'total_entries', 'last_at', 'closed_win_rate'):
        raise ReadError(400, 'Invalid sort')
    size = integer(query, 'page_size', 24, 1, 100)
    page = integer(query, 'page', 1, 1, 1000000)
    rows = snapshot['catalog']
    def matches(item):
        r = item['report']; c = r['config']
        low, high = c['entry_cap_min'], c['entry_cap_max']
        searchable = ' '.join(str(x or '') for x in (item['id'], item['label'], item['family'],
            (item.get('trial') or {}).get('label'), (item.get('trial') or {}).get('parent_id'),
            c.get('entry_style'), low, high, f'{low/1000:g}k-{high/1000:g}k', f'{low/1000:g}k–{high/1000:g}k'))
        status = r.get('experiment_status') or ('active' if r.get('total_entries') else 'observing')
        wins = r.get('closed_win_rate')
        outcomes = {'': True, 'profitable': r['pnl_usd'] > 0, 'unprofitable': r['pnl_usd'] < 0,
            'positive_24h': r.get('pnl_24h_usd', 0) > 0,
            'winning_record': r.get('closed_count', 0) > 0 and wins is not None and wins >= .5,
            'losing_record': r.get('closed_count', 0) > 0 and wins is not None and wins < .5,
            'awaiting_record': r.get('closed_count') == 0}
        return (q.lower() in searchable.lower() and (not family or item['family'] == family)
            and (not state or status == state) and outcomes[outcome]
            and (not version or bool(item.get('trial')) == (version == 'trials')))
    filtered = [item for item in rows if matches(item)]
    def key(item):
        r = item['report']
        v = (r['pnl_usd'] / r['initial_usd'] if r.get('initial_usd') else None) if metric == 'return_pct' else r.get(metric)
        # Missing win rate always comes last, not ahead of a known zero.
        return (v is None, (v or 0) * (1 if ascending else -1), item['label'], item['id'])
    filtered.sort(key=key)
    page = min(page, max(1, math.ceil(len(filtered) / size)))
    ids = list(dict.fromkeys(filter(None, value('compare').split(','))))
    if len(ids) > 4 or any(not ID.fullmatch(i) for i in ids):
        raise ReadError(400, 'Invalid comparison selection')
    by_id = {item['id']: item for item in rows}
    return {'strategies': filtered[(page-1)*size:page*size], 'total': len(filtered), 'page': page, 'page_size': size,
            'families': sorted({item['family'] for item in rows if item.get('family')}),
            'comparison': [by_id[i] for i in ids if i in by_id], 'activity': snapshot['activity']}


class PublicReadService:
    def __init__(self, brain, interval=15, stale_after=45, autostart=True):
        self.brain, self.interval, self.stale_after = brain, interval, stale_after
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.generation = None
        self.last_error = False
        self.history_slots = threading.BoundedSemaphore(4)
        self.export_slot = threading.Lock()
        self.export_cache = {}
        self.thread = threading.Thread(target=self._loop, name='public-snapshot', daemon=True)
        if autostart:
            self.thread.start()

    def close(self):
        self.stop.set()
        if self.thread.is_alive():
            self.thread.join(timeout=2)

    def _loop(self):
        while not self.stop.is_set():
            started=time.monotonic()
            self.refresh_once()
            # Include build time in the cadence. Adding a full delay after a
            # long build needlessly ages both snapshots and collector heartbeats.
            self.stop.wait(max(1,self.interval-(time.monotonic()-started)))

    def refresh_once(self):
        try:
            if hasattr(self.brain, 'reload_public_registry'):
                self.brain.reload_public_registry()
            started = time.time()
            with read_brain(self.brain) as shadow:
                document = shadow.public_view(started)
                feed = shadow.live_feed_view(started)
            catalog_rows = [brief(s) for s in document['strategies']]
            activity = {'instances': len(catalog_rows), 'dead': sum(dead(s['report']) for s in catalog_rows),
                'events_24h': sum(s['report']['activity_counts']['today'] for s in catalog_rows),
                'entries_24h': sum(s['report']['trade_counts']['today'] for s in catalog_rows),
                'open_positions': len(document['open_positions'])}
            summary = {key:value for key,value in document.items() if key not in ('strategies', 'research')}
            summary['strategies'] = catalog_rows
            generation = {'document':document, 'catalog':catalog_rows, 'activity':activity,
                'by_id':{s['id']:s for s in document['strategies']}, 'started':started, 'published':time.time(),
                'bodies':{'/api/public':encode(document), '/api/public/summary':encode(summary),
                    '/api/public/research':encode({'research':document['research']}), '/api/public/live-feed':encode(feed)}}
            generation['bodies']['/api/public/community'] = encode(document['community'])
            with self.lock:
                self.generation, self.last_error = generation, False
            return True
        except Exception:
            logging.getLogger(__name__).exception('Public snapshot refresh failed; retaining previous publication')
            # Keep last known good publication; never expose SQL paths/errors publicly.
            with self.lock:
                self.last_error = True
            return False

    def snapshot(self):
        with self.lock:
            generation, failed = self.generation, self.last_error
        if generation is None:
            raise ReadError(503, 'Public snapshot is warming up. Retry shortly.')
        age = max(0, time.time() - generation['started'])
        metadata = {'generated_at':generation['published'], 'source_as_of':generation['started'],
            'age_seconds':round(age, 1), 'stale':failed or age > self.stale_after, 'refresh_failed':failed,
            'refresh_interval_seconds':self.interval, 'consistency':'Per-ledger committed reads; not atomic across instances.'}
        return generation, metadata

    def health(self):
        """Constant-time readiness: no ledger queries, trade-count heuristics or secrets."""
        now = time.time()
        try:
            _, meta = self.snapshot()
            snapshot_ok, snapshot_age = not meta['stale'], meta['age_seconds']
        except ReadError:
            snapshot_ok, snapshot_age = False, None
        timing = dict(self.brain.research_timing)
        def age(key):
            value = timing.get(key)
            return round(max(0, now-value), 1) if isinstance(value, (int, float)) and value > 0 else None
        cycle_age, input_age = age('completed_at'), age('input_observed_at')
        checks = {'snapshot': snapshot_ok, 'snapshot_worker': self.thread.is_alive(),
                  'research_cycle': cycle_age is not None and cycle_age <= 180,
                  'market_input': input_age is not None and input_age <= 300,
                  'workers': bool(getattr(self.brain, 'threads', [])) and all(t.is_alive() for t in self.brain.threads)}
        return {'status': 'ok' if all(checks.values()) else 'degraded', 'checks': checks,
                'snapshot_age_seconds': snapshot_age, 'research_cycle_age_seconds': cycle_age,
                'market_input_age_seconds': input_age,
                'thresholds_seconds': {'snapshot': self.stale_after, 'research_cycle': 180, 'market_input': 300}}

    def response(self, path, query):
        generation, metadata = self.snapshot()
        if path in generation['bodies']:
            body = generation['bodies'][path]
        elif path == '/api/public/catalog':
            body = encode(catalog(generation, query))
        elif path.startswith('/api/public/strategies/') and not path.endswith('/trades'):
            id = path[len('/api/public/strategies/'):]
            s = generation['by_id'].get(id)
            if s is None:
                raise ReadError(404, 'Instance not found')
            trial = s.get('trial') or next((x['trial'] for x in generation['catalog'] if (x.get('trial') or {}).get('parent_id') == id), None)
            related = [s]
            if trial:
                related += [x for x in generation['catalog'] if x['id'] != id and (x['id'] == trial['parent_id'] or
                    ((x.get('trial') or {}).get('parent_id') == trial['parent_id'] and (x.get('trial') or {}).get('generation') == trial.get('generation')))]
            body = encode({'strategies':related, 'open_positions':[p for p in generation['document']['open_positions'] if p['strategy_id'] == id],
                           'closed_positions':[], 'as_of':generation['started']})
        else:
            raise ReadError(404, 'Public endpoint not found')
        return b'{"_snapshot":' + encode(metadata) + b',' + body[1:]

    def history(self, id, query):
        generation, metadata = self.snapshot()
        if not ID.fullmatch(id) or id not in generation['by_id']:
            raise ReadError(404, 'Instance not found')
        size = integer(query, 'limit', 100, 1, 200)
        anchor = integer(query, 'snapshot_max_id', 0, 0, 2**63-1)
        before = integer(query, 'before', 2**63-1, 1, 2**63-1)
        if not self.history_slots.acquire(blocking=False):
            raise ReadError(429, 'History service is busy. Retry shortly.')
        try:
            # Registry lookup only: no runner queries or writer locks.
            with self.brain.strategy_lock:
                item = next((s for s in [self.brain.primary_strategy] + self.brain.experiment_runners if s['id'] == id), None)
            if item is None:
                raise ReadError(404, 'Instance not found')
            with ExitStack() as stack:
                con = readonly(item['runner'].path, time.monotonic()+3)
                stack.callback(con.close)
                con.execute('BEGIN')
                if 'snapshot_max_id' not in query:
                    anchor = con.execute('SELECT coalesce(max(id),0) FROM trades').fetchone()[0]
                total = con.execute('SELECT count(*) FROM trades WHERE id<=?', (anchor,)).fetchone()[0]
                rows = con.execute('SELECT id,data FROM trades WHERE id<=? AND id<? ORDER BY id DESC LIMIT ?', (anchor,before,size+1)).fetchall()
                # Reuse the public ledger mapping on a bounded in-memory query result.
                trades = []
                for row in rows[:size]:
                    t = json.loads(row['data'])
                    trades.append({'id':row['id'], 'strategy_id':id,'strategy_label':item['label'],
                        **{key:t.get(key) for key in ('mint','symbol','entry_at','exit_at','entry_cap','pnl_usd','cost_usd','proceeds_usd',
                            'entry_price_usd','exit_price_usd','exit_fees_usd','exit_reason')}, 'exit_cap':t.get('last_cap')})
                return encode({'strategy_id':id,'trades':trades,'total':total,'snapshot_max_id':anchor,
                    'next_before':rows[size-1]['id'] if len(rows)>size else None,'limit':size,'_snapshot':metadata})
        except sqlite3.Error:
            raise ReadError(503, 'History temporarily unavailable')
        finally:
            self.history_slots.release()

    def export(self, path):
        """Small complete CSV exports only; never silently truncate large datasets."""
        if not self.export_slot.acquire(blocking=False):
            raise ReadError(429, 'An export is already being prepared')
        try:
            cached = self.export_cache.get(path)
            if cached and time.monotonic()-cached[0] < 60:
                if isinstance(cached[1], ReadError): raise cached[1]
                return cached[1]
            try:
                with read_brain(self.brain, timeout=10) as shadow:
                    count = 0
                    sources = [(shadow.memory.db, ('research_bars','research_events','research_outcomes'))] if path.endswith('research.csv') else [
                        (item['runner'].db, ('events','sales','trades','equity')) for item in [shadow.primary_strategy]+shadow.experiment_runners]
                    for con, tables in sources:
                        for table in tables:
                            count += con.execute(f'SELECT count(*) FROM (SELECT 1 FROM {table} LIMIT 10001)').fetchone()[0]
                            if count > 10000:
                                raise ReadError(413, 'Dataset exceeds the 10,000-row public CSV limit. Use per-instance research downloads or an operator-prepared archive.')
                    body = (shadow.export_research_csv() if path.endswith('research.csv') else shadow.export_public_history_csv()).encode()
                    if len(body) > 20_000_000:
                        raise ReadError(413, 'Export exceeds the 20 MB public limit')
                self.export_cache[path] = (time.monotonic(), body)
                return body
            except (ReadError, sqlite3.Error) as exc:
                error = exc if isinstance(exc, ReadError) else ReadError(503, 'Export timed out; request an operator-prepared archive')
                self.export_cache[path] = (time.monotonic(), error)
                raise error
        finally:
            self.export_slot.release()
