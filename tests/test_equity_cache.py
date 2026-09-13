import copy
import json
import time
from test_public_read import PublicReadTests
from paper.equity_cache import EquityCache


class EquityCacheTests(PublicReadTests):
    def test_incremental_results_equal_full_sql_at_boundaries(self):
        now=time.time()
        with self.runner.db:
            for index in range(100):
                self.runner.db.execute('INSERT INTO equity(at,value,cash) VALUES (?,?,?)',(now-100000+index*1600,500+index,500))
        def compare(at):
            self.brain.public_equity_cache=None
            strategies=self.brain.public_strategies(at)
            expected=self.brain.portfolio_view(strategies,at)
            self.brain.public_equity_cache=cache
            actual=self.brain.portfolio_view(strategies,at)
            self.assertEqual(actual,expected)
        cache=EquityCache(); compare(now)
        with self.runner.db: self.runner.db.execute('INSERT INTO equity(at,value,cash) VALUES (?,?,?)',(now+160000,900,900))
        compare(now+170000)
        with self.runner.db: self.runner.db.execute('DELETE FROM equity')
        compare(now+180000)
