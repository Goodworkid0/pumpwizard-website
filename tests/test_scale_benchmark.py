import unittest
from unittest.mock import patch
from scripts.benchmark_scale import run, stats


class ScaleBenchmarkTests(unittest.TestCase):
    def test_measures_real_cycle_publication_reads_and_recovery_without_network(self):
        with patch('socket.socket', side_effect=AssertionError('No network')):
            result = run(2, 3, 1)
        self.assertEqual(result['instances'],2)
        self.assertEqual(result['cycle']['samples'],3)
        self.assertGreater(result['publication']['samples'],0)
        self.assertGreater(result['concurrent_reads']['samples'],0)
        self.assertEqual(result['read_errors'],0)
        self.assertTrue(all(result['gates'].values()))
        self.assertGreater(result['working_set_bytes_after'],result['working_set_bytes_before'])

    def test_low_descriptor_limit_fails_before_allocating_instances(self):
        with patch('resource.getrlimit',return_value=(100,100)):
            with self.assertRaisesRegex(ValueError,'descriptor'): run(1000,3,1)

    def test_percentiles_are_nearest_rank(self):
        self.assertEqual(stats([.001,.002,.003])['p95_ms'],3)
        self.assertIsNone(stats([]))
