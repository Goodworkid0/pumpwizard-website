import copy
import hashlib
import gzip
import base64
import zlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from paper.engine import canonical
from paper.experiment_catalog import catalog
from paper.research_runner import ResearchRunner
from paper.shared_research import SharedResearch
from paper.replay_package import create, verify, decode_input, Output
from paper.versioned_trials import TrialResearchRunner, TrialLadderRunner
from paper.forward_trials_v3 import V3ResearchRunner, V3LadderRunner
from paper.ladder import LadderRunner

ROOT = Path(__file__).resolve().parents[1]


def quote(at, cap):
    return {'mint': 'A'*44, 'symbol': 'TEST', 'observed_at': at, 'dex_id': 'pumpswap',
            'pair_address': 'pool', 'valuation_basis': 'fdv', 'market_cap_usd': cap,
            'price_usd': cap/1e9, 'base_reserve': 200000000, 'liquidity_usd': cap*.4,
            'source_timestamp_known': False}


def fixture(root, cls=ResearchRunner, definition=None):
    base = json.loads((ROOT/'long_hold_experiments.json').read_text())['experiments'][0]['config']
    config = copy.deepcopy(next(e['config'] for e in catalog(base) if e['family']=='persistent'
                               and e['pilot'] and e['config']['entry_cap_min']==100000))
    config['failed_attempt_probability'] = 0
    risk = json.loads((ROOT/'ownership.config.json').read_text())
    shared = SharedResearch(root/'shared.sqlite')
    if not issubclass(cls, ResearchRunner):
        config['entry_style'] = 'momentum'
    args = [root/'ledger.sqlite',config,risk]
    if issubclass(cls, ResearchRunner): args.append(shared)
    runner = cls(*args, source='synthetic_test', **({'definition':definition} if definition else {}))
    for i in range(18):
        at = 1000+i*30
        cap = 150000+i*1000 if i<15 else 80000
        batch = {'at':at, 'quotes':[quote(at,cap)], 'evidence':{}}
        shared.prepare(batch,runner.market); runner.step(batch)
    runner.close(); shared.close()


class ReplayPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name); fixture(self.root)

    def build(self, name='package'):
        return create(self.root/'ledger.sqlite', self.root/'shared.sqlite', self.root/name, frozen_source=True)

    def test_exact_replay_and_separate_features_outcomes_without_network(self):
        before = {n:hashlib.sha256((self.root/n).read_bytes()).hexdigest() for n in ('ledger.sqlite','shared.sqlite')}
        with patch('socket.socket', side_effect=AssertionError('No network permitted')):
            result = self.build()
            proof = verify(self.root/'package')
        self.assertEqual(result['tables'], proof['tables'])
        self.assertEqual(result['tables']['trades']['rows'], 1)
        with gzip.open(self.root/'package/decisions.jsonl.gz','rt') as stream:
            decisions = [json.loads(line) for line in stream]
        self.assertTrue(any(d['signal_passed'] for d in decisions))
        self.assertTrue(any(not d['signal_passed'] for d in decisions))
        self.assertTrue(all('price_return' not in d for d in decisions))
        self.assertTrue((self.root/'package/outcomes.jsonl.gz').exists())
        with gzip.open(self.root/'package/outcomes.jsonl.gz','rt') as stream:
            outcomes = [json.loads(line) for line in stream]
        self.assertTrue(any(o['quality']=='observed_within_120s' for o in outcomes))
        self.assertTrue(any(o['quality']=='not_observed_in_package' for o in outcomes))
        for outcome in outcomes:
            if outcome['observed_at'] is not None:
                self.assertLessEqual(outcome['due_at'],outcome['observed_at'])
                self.assertLessEqual(outcome['observed_at'],outcome['due_at']+120)
            else: self.assertIsNone(outcome['price_return'])
        for n,h in before.items(): self.assertEqual(hashlib.sha256((self.root/n).read_bytes()).hexdigest(),h)

    def test_missing_shared_input_refuses_complete_package(self):
        with sqlite3.connect(self.root/'shared.sqlite') as con:
            con.execute('DELETE FROM inputs WHERE at=(SELECT min(at) FROM inputs)')
        con.close()
        with self.assertRaisesRegex(ValueError,'missing'): self.build()
        self.assertFalse((self.root/'package/manifest.json').exists())

    def test_tampering_and_reused_destination_rejected(self):
        self.build()
        with self.assertRaises(FileExistsError): self.build()
        (self.root/'package/decisions.jsonl.gz').write_text('{}\n')
        with self.assertRaisesRegex(ValueError,'integrity'): verify(self.root/'package')

    def test_rehashed_false_derived_evidence_is_rejected(self):
        self.build()
        path = self.root/'package/decisions.jsonl.gz'
        with gzip.open(path,'rt') as stream: data = [json.loads(line) for line in stream]
        data[0]['signal_passed'] = not data[0]['signal_passed']
        with gzip.open(path,'wt') as stream: stream.write(''.join(canonical(d)+'\n' for d in data))
        manifest_path = self.root/'package/manifest.json'
        manifest = json.loads(manifest_path.read_text())
        manifest['files']['decisions.jsonl.gz'] = {'bytes':path.stat().st_size,
                                              'sha256':hashlib.sha256(path.read_bytes()).hexdigest()}
        manifest_path.write_text(canonical(manifest))
        with self.assertRaisesRegex(ValueError,'Derived evidence'): verify(self.root/'package')

    def test_compressed_input_bound_and_hash(self):
        raw = canonical({'at':1,'payload':'x'*2048}).encode()
        row = {'id':hashlib.sha256(raw).hexdigest(),'at':1,
               'payload_zlib_base64':base64.b64encode(zlib.compress(raw)).decode()}
        with patch('paper.replay_package.MAX_BYTES',1024):
            with self.assertRaisesRegex(ValueError,'oversized'): decode_input(row)
        row['id']='0'*64
        with self.assertRaisesRegex(ValueError,'hash'): decode_input(row)

    def test_manifest_is_not_written_over_disk_budget(self):
        folder = self.root/'bounded'; folder.mkdir()
        output = Output(folder, 100)
        output.write('existing.json', 'x'*80)
        with self.assertRaisesRegex(ValueError, 'size budget'):
            output.write('manifest.json', {'complete': True})
        self.assertFalse((folder/'manifest.json').exists())

    def test_versioned_and_legacy_runner_support(self):
        v2 = {'trial_id':'synthetic','group':'persistent','arm':'combined','label':'combined',
              'parent_id':'original','control_id':'control','risk_enabled':False,'ownership_required':False,
              'ownership_telemetry':True,'exits_enabled':True,'profit_activation_return':.2,'profit_trailing_drawdown':.1}
        v3 = {'trial_id':'synthetic','generation':'V3','comparison_title':'Test','group':'persistent',
              'arm':'combined','label':'combined','parent_id':'original','control_id':'control',
              'confirmation_enabled':True,'protection_enabled':True,'confirmation_delay_seconds':60,
              'break_even_activation_return':.1}
        for cls,definition in ((TrialResearchRunner,v2),(V3ResearchRunner,v3),(LadderRunner,None),
                               (TrialLadderRunner,v2),(V3LadderRunner,v3)):
            root = self.root/cls.__name__; root.mkdir()
            fixture(root,cls,definition)
            create(root/'ledger.sqlite',root/'shared.sqlite',root/'package',frozen_source=True)
            self.assertEqual(verify(root/'package')['status'],'verified')

    def test_contract_drift_and_incomplete_history_fail_closed(self):
        with sqlite3.connect(self.root/'ledger.sqlite') as con:
            con.execute('DELETE FROM input_refs WHERE at=(SELECT min(at) FROM input_refs)')
        con.close()
        with self.assertRaises(ValueError): self.build()
        self.assertFalse((self.root/'package/manifest.json').exists())

    def test_frozen_assertion_wal_and_size_limits(self):
        with self.assertRaisesRegex(ValueError,'frozen'):
            create(self.root/'ledger.sqlite',self.root/'shared.sqlite',self.root/'absent')
        self.assertFalse((self.root/'absent').exists())
        with self.assertRaisesRegex(ValueError,'count'):
            create(self.root/'ledger.sqlite',self.root/'shared.sqlite',self.root/'small',frozen_source=True,max_inputs=1)
        self.assertFalse((self.root/'small/manifest.json').exists())
        (self.root/'ledger.sqlite-wal').write_bytes(b'not frozen')
        with self.assertRaisesRegex(ValueError,'WAL'): self.build('wal')
