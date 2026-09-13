"""Separate public process: committed read-only ledgers, no collector or RPC client."""
import hashlib
import json
import os
from pathlib import Path
import signal
import threading
import time

from .brain_service import BrainService
from .memory import Memory
from .engine import canonical
from .public_read import ReadDatabase, ReadTransaction, ID
from .replay_package import RUNNERS

SCHEMA = 'pumpwizard.public-registry/v1'


def publish_registry(brain):
    with brain.strategy_lock:
        entries = []
        for item in [brain.primary_strategy] + brain.experiment_runners:
            runner = item['runner']
            entries.append({k: item.get(k) for k in ('id', 'label', 'trial', 'description', 'family')} | {
                'ledger': runner.path.name, 'runner': type(runner).__name__,
                'contract': runner.contract})
    payload = {'schema': SCHEMA, 'at': time.time(), 'entries': entries,
               'ledger_backend':'postgres' if getattr(brain,'runtime_backend',None) else 'sqlite',
               'timing': dict(brain.research_timing), 'status': dict(brain.status),
               'workers_alive': bool(brain.threads) and all(t.is_alive() for t in brain.threads),
               'shared': bool(brain.shared),
               'catalog': [{'pilot': r['pilot']} for r in brain.research_catalog]}
    path = brain.root/'data/public-registry.json'
    temporary = path.with_suffix('.tmp')
    with temporary.open('w') as stream:
        json.dump(payload, stream, allow_nan=False)
        stream.flush(); os.fsync(stream.fileno())
    temporary.replace(path)


class RegistryPublisher:
    def __init__(self, brain):
        self.brain = brain
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self.loop, name='public-registry', daemon=True)
        self.thread.start()

    def loop(self):
        import logging
        while not self.stop.is_set():
            try: publish_registry(self.brain)
            except Exception: logging.getLogger(__name__).exception('Public registry publication failed')
            self.stop.wait(5)

    def close(self):
        self.stop.set(); self.thread.join(timeout=6)


class CollectorHeartbeat:
    def __init__(self, document): self.document = document
    def is_alive(self):
        return self.document['workers_alive'] is True and 0 <= time.time()-self.document['at'] <= 30


class PublicBrain(BrainService):
    """Reuse only report methods. Never call BrainService/runner/Memory constructors."""
    def __init__(self, root):
        self.root = Path(root)
        self.strategy_lock = threading.RLock()
        self.community_path = self.root/'community.config.json'
        self.ladder_enabled = True
        self.sampler = None
        from .equity_cache import EquityCache
        self.public_equity_cache = EquityCache()
        self.config = json.loads((self.root/'brain.config.json').read_text())
        self.memory = Memory.__new__(Memory)
        self.memory.config = self.config
        self.memory.db = ReadDatabase(self.root/'data/coin-memory.sqlite', None)
        self.memory.lock = ReadTransaction(self.memory.db)
        self.reload_public_registry()

    def reload_public_registry(self):
        path = Path(os.environ.get('PUMP_PUBLIC_REGISTRY',self.root/'data/public-registry.json'))
        if path.is_symlink() or path.stat().st_size > 10_000_000:
            raise ValueError('Invalid public registry')
        document = json.loads(path.read_text())
        if document['schema'] != SCHEMA or not 0 <= time.time()-document['at'] <= 30:
            raise ValueError('Collector registry is stale')
        classes = {cls.__name__: cls for cls in RUNNERS}
        entries = []; seen = set()
        for item in document['entries']:
            ident = item['id']
            if not ID.fullmatch(ident) or ident in seen or item['ledger'] != 'long-hold-'+ident+'.sqlite':
                raise ValueError('Invalid registered ledger')
            seen.add(ident)
            cls = classes[item['runner']]
            runner = cls.__new__(cls)
            runner.path = self.root/'data'/item['ledger']
            if runner.path.is_symlink(): raise ValueError('Symlink ledger refused')
            runner.contract = item['contract']; runner.cfg = runner.contract['config']
            runner.policy_hash = hashlib.sha256(canonical(runner.contract['ownership_config']).encode()).hexdigest()
            runner.experiment_definition = runner.contract.get('experiment')
            runner.state = {}
            runner.db = ReadDatabase(runner.path, None)
            runner.lock = ReadTransaction(runner.db, runner)
            entries.append({k: item.get(k) for k in ('id', 'label', 'trial', 'description', 'family')} | {'runner': runner})
        if not entries: raise ValueError('Empty registry')
        with self.strategy_lock:
            self.primary_strategy, self.experiment_runners = entries[0], entries[1:]
            self.runner = entries[0]['runner']
            self.research_catalog = document['catalog']; self.shared = document['shared']
            self.status, self.research_timing = document['status'], document['timing']
            self.threads = [CollectorHeartbeat(document)]


def main():
    import argparse
    from .server import serve
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--bind', default='127.0.0.1')
    args = parser.parse_args()
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT): signal.signal(sig, lambda *_: stop.set())
    # Container restart policy handles missing/invalid first publication.
    brain = PublicBrain(args.root)
    server = serve(None, args.port, brain, bind_host=args.bind)
    try: stop.wait()
    finally: server.shutdown(); server.server_close()


if __name__ == '__main__': main()
