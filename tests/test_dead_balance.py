import unittest
from paper.public_read import dead


class DeadBalanceTests(unittest.TestCase):
    def test_threshold_is_strict_and_independent_of_activity(self):
        for balance in (-10, 0, 10, 49, 49.999, 50, 50.01, 500):
            self.assertEqual(dead({'equity_usd': balance}), balance < 50)
            self.assertEqual(dead({'equity_usd': balance, 'cash_usd': 0,
                                   'open_position_count': 3, 'pending_count': 1}), balance < 50)

    def test_unknown_balance_is_not_dead(self):
        for report in (None, {}, *({'equity_usd': n} for n in
                     (None, True, False, '49', float('nan'), float('inf'), -float('inf')))):
            self.assertFalse(dead(report))
