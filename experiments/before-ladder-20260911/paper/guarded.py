"""Replayable buy gate layered over the frozen simulator; exits use the base engine."""
import hashlib
import json
import zlib
from pathlib import Path

from .engine import Engine, canonical


class GuardedEngine(Engine):
    def __init__(self, db_path, config, source, ownership_config, view_only=False, policy=None):
        super().__init__(db_path, config, source, view_only=view_only)
        code_hash = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
        policy_hash = hashlib.sha256(canonical(ownership_config).encode()).hexdigest()
        stored = self.db.execute("SELECT value FROM meta WHERE key='entry_policy'").fetchone()
        self.entry_policy = json.loads(stored[0]) if stored else policy or {
            'version': 'ownership-v1', 'code_hash': code_hash,
            'policy_hash': policy_hash, 'config': ownership_config,
            'effective_after': self.state['last_at'] or 0}
        if not view_only and (self.entry_policy['code_hash'] != code_hash or
                              self.entry_policy['policy_hash'] != policy_hash):
            self.close()
            raise ValueError('Entry policy changed; preserve the previous policy and use a separate experiment')
        if not stored and not view_only:
            with self.db:
                self.db.execute('INSERT INTO meta VALUES (?,?)',('entry_policy',canonical(self.entry_policy)))

    def _entry_quote(self, snapshot, t):
        quote, reason = super()._entry_quote(snapshot,t)
        if reason or t <= self.entry_policy['effective_after']:
            return quote, reason
        gate = (snapshot or {}).get('entry_guard') or {}
        checked = gate.get('checked_at')
        if (gate.get('mint') != snapshot['mint'] or
                gate.get('policy_hash') != self.entry_policy['policy_hash'] or
                not isinstance(checked,(float,int)) or not 0 <= t-checked <= self.entry_policy['config']['check_ttl_seconds']):
            return None,'ownership_unverified'
        if gate.get('status') != 'clear':
            return None,'ownership_blocked' if gate.get('status') == 'blocked' else 'ownership_unverified'
        return quote,None

    def report(self):
        result = super().report()
        result['entry_policy'] = self.entry_policy
        return result

    def export_recording(self,path):
        with self.lock,open(path,'w') as f:
            f.write(canonical({'type':'header','source':self.source,'config':self.config,
                'implementation_hash':self.implementation_hash,'entry_policy':self.entry_policy})+'\n')
            for row in self.db.execute('SELECT payload FROM batches ORDER BY at'):
                f.write(zlib.decompress(row[0]).decode()+'\n')
