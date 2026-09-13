"""Synthetic fixtures exercise execution. They are never profitability evidence."""
import math


def batches():
    origin = 1789000000.0
    for elapsed in range(0, 751, 5):
        snaps = []
        for mint, symbol in [('DemoRise', 'DEMO RISE'), ('DemoFall', 'DEMO FALL'),
                             ('DemoGap', 'DEMO GAP')]:
            if mint == 'DemoRise':
                factor = 1 + elapsed * 0.003
            elif mint == 'DemoFall':
                factor = 1 + min(elapsed, 90) * 0.003
                if elapsed > 90:
                    factor = max(0.65, factor - (elapsed - 90) * 0.009)
            else:
                factor = 1 + min(elapsed, 100) * 0.003
            x = 500_000_000 / math.sqrt(factor)
            y = 64.38 * math.sqrt(factor)
            now = origin + elapsed
            snaps.append({'mint': mint, 'symbol': symbol, 'observed_at': now,
                'source_at': now if mint != 'DemoGap' or elapsed <= 100 else origin + 100,
                'fx_at': now, 'quote_usd': 100, 'x': x, 'y': y,
                'real_x': x - 279_900_000, 'real_y': y - 30, 'supply': 1_000_000_000,
                'market_cap_usd': y / x * 1e9 * 100, 'decimals': 6, 'complete': False,
                'route': 'pump_bonding_curve_sol', 'mayhem': False, 'source': 'synthetic_demo'})
        yield {'at': now, 'snapshots': snaps, 'status': 'synthetic',
               'issues': [], 'discovered_count': 3}
