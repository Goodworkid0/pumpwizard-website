"""Deterministic, approximate constant-product quotes in human token units.

This is a sensitivity model over indexed snapshots, not a transaction simulator.
Fee settings are frozen experiment assumptions; no fees are actually paid.
"""
import json
import math
from pathlib import Path


def load_config(path):
    cfg = json.loads(Path(path).read_text())
    return validate_config(cfg)


def validate_config(cfg):
    # Original recordings predate re-entry and watch rotation settings.
    cfg = dict(cfg)
    for key, value in {'max_entry_attempts_per_mint': 1, 'reentry_cooldown_seconds': 120,
                       'watch_stale_seconds': 45, 'watch_out_of_range_seconds': 90}.items():
        cfg.setdefault(key, value)
    defaults = json.loads((Path(__file__).parent.parent / 'paper.config.json').read_text())
    if set(cfg) != set(defaults):
        raise ValueError('Configuration keys must match paper.config.json exactly')
    if cfg['mode'] != 'paper_only' or cfg['strategy_id'] != 'snapshot_momentum_v1':
        raise ValueError('Only the paper snapshot strategy is implemented')
    for k, v in cfg.items():
        if k not in ('mode', 'strategy_id') and (isinstance(v, bool) or
                not isinstance(v, (int, float)) or not math.isfinite(v)):
            raise ValueError(f'Invalid numeric setting: {k}')
    for k, v in cfg.items():
        if isinstance(v, (int, float)) and k != 'stop_loss_fraction' and v < 0:
            raise ValueError(f'{k} cannot be negative')
    for k in ('virtual_balance_usd', 'position_usd', 'max_exposure_usd',
              'observation_seconds', 'short_window_seconds', 'max_hold_seconds',
              'poll_seconds', 'discovery_seconds', 'max_source_age_seconds',
              'max_observation_age_seconds', 'max_sample_gap_seconds', 'order_expiry_seconds',
              'reentry_cooldown_seconds', 'watch_stale_seconds', 'watch_out_of_range_seconds'):
        if cfg[k] <= 0:
            raise ValueError(f'{k} must be positive')
    for k in ('max_positions', 'min_samples', 'max_watch_tokens', 'random_seed', 'max_entry_attempts_per_mint'):
        if not isinstance(cfg[k], int) or cfg[k] < 1:
            raise ValueError(f'{k} must be a positive integer')
    if not (cfg['position_usd'] <= cfg['max_exposure_usd'] <= cfg['virtual_balance_usd']):
        raise ValueError('Position, exposure and balance are inconsistent')
    if not (0 < cfg['market_cap_min_usd'] < cfg['market_cap_max_usd']):
        raise ValueError('Invalid market cap range')
    if not (-1 < cfg['stop_loss_fraction'] < 0 < cfg['take_profit_fraction']):
        raise ValueError('Invalid exit thresholds')
    if not (0 < cfg['short_window_seconds'] < cfg['observation_seconds']):
        raise ValueError('Short observation window must be smaller')
    if cfg['max_watch_tokens'] < cfg['max_positions'] or cfg['max_watch_tokens'] > 100:
        raise ValueError('Watch limit must cover positions and be at most 100')
    for k in ('failed_attempt_probability', 'max_price_impact_fraction',
              'max_entry_real_reserve_fraction', 'max_roundtrip_cost_fraction',
              'daily_loss_halt_fraction', 'drawdown_halt_fraction'):
        if not 0 <= cfg[k] < 1:
            raise ValueError(f'{k} must be in [0, 1)')
    if any(cfg[k] >= 1000 for k in ('protocol_fee_bps', 'route_fee_bps', 'adverse_fill_bps')):
        raise ValueError('Fee / adverse fill assumption must be less than 10% per component')
    if cfg['entry_latency_seconds'] >= cfg['order_expiry_seconds']:
        raise ValueError('Entry latency must be below order expiry')
    return cfg


def usable(s, now, cfg):
    if not s:
        return 'missing_snapshot'
    if s.get('validation_error'):
        return s['validation_error']
    for k in ('observed_at', 'source_at', 'fx_at', 'quote_usd', 'x', 'y',
              'real_x', 'real_y', 'supply', 'market_cap_usd', 'decimals'):
        if not isinstance(s.get(k), (float, int)) or not math.isfinite(s[k]):
            return 'invalid_' + k
    for k in ('observed_at', 'source_at', 'fx_at'):
        if s[k] > now + 2:
            return 'future_timestamp'
    if now - s['observed_at'] > cfg['max_observation_age_seconds']:
        return 'observation_stale'
    if now - min(s['source_at'], s['fx_at']) > cfg['max_source_age_seconds']:
        return 'source_stale'
    if s.get('route') != 'pump_bonding_curve_sol':
        return 'unsupported_route'
    if s.get('mayhem', False):
        return 'unsupported_mayhem_curve'
    if s.get('complete', True) or s['real_x'] <= 0:
        return 'curve_completed_or_migrated'
    if min(s['quote_usd'], s['x'], s['y'], s['supply']) <= 0:
        return 'invalid_reserves'
    if s['real_y'] < 0 or s['real_y'] > s['y'] or s['real_x'] > s['x']:
        return 'inconsistent_reserves'
    if not 0 <= s['decimals'] <= 12 or s['decimals'] != int(s['decimals']):
        return 'unsupported_decimals'
    implied = s['y'] / s['x'] * s['supply'] * s['quote_usd']
    if abs(s['market_cap_usd'] / implied - 1) > 0.10:
        return 'valuation_reserve_mismatch'
    return None


def buy_quote(s, budget, cfg):
    """Budget includes all modeled entry costs. Quantity rounded down."""
    fx = s['quote_usd']
    fixed = (cfg['network_fee_sol'] + cfg['entry_rent_sol']) * fx
    fee_rate = (cfg['protocol_fee_bps'] + cfg['route_fee_bps']) / 10000
    pool_in = (budget - fixed) / fx / (1 + fee_rate)
    if pool_in <= 0:
        return None, 'entry_cost_exceeds_budget'
    before_haircut = s['x'] * pool_in / (s['y'] + pool_in)
    qty = math.floor(before_haircut * (1 - cfg['adverse_fill_bps'] / 10000)
                     * 10 ** s['decimals']) / 10 ** s['decimals']
    if qty <= 0 or before_haircut >= s['real_x']:
        return None, 'insufficient_token_reserves'
    impact = pool_in / before_haircut / (s['y'] / s['x']) - 1
    if impact > cfg['max_price_impact_fraction']:
        return None, 'entry_price_impact'
    if s['real_y'] * fx < cfg['min_real_reserves_usd']:
        return None, 'insufficient_real_liquidity'
    if pool_in > s['real_y'] * cfg['max_entry_real_reserve_fraction']:
        return None, 'position_too_large_for_real_liquidity'
    return {'quantity': qty, 'cost_usd': budget, 'pool_in_sol': pool_in,
            'entry_price_usd': budget / qty, 'entry_price_sol': budget / fx / qty,
            'entry_quote_usd': fx, 'entry_impact_fraction': impact,
            'entry_fees_usd': fixed + pool_in * fx * fee_rate}, None


def sell_quote(s, p, cfg):
    """Counterfactual own-position reserve offset, reanchored to each snapshot.

    External snapshot x,y -> shadow x-q, k/(x-q). Our token purchase remains
    reflected in this shadow curve. This cannot predict other traders' reactions.
    """
    qty = p['quantity']
    if qty <= 0 or qty >= s['x']:
        return None, 'invalid_exit_quantity'
    shadow_x = s['x'] - qty
    shadow_y = s['x'] * s['y'] / shadow_x
    shadow_real_y = s['real_y'] + shadow_y - s['y']
    gross = shadow_y * qty / (shadow_x + qty)
    if s['real_y'] <= 0 or gross > shadow_real_y:
        return None, 'insufficient_exit_liquidity'
    gross *= (1 - cfg['adverse_fill_bps'] / 10000)
    fee_rate = (cfg['protocol_fee_bps'] + cfg['route_fee_bps']) / 10000
    fees = gross * fee_rate + cfg['network_fee_sol']
    net = (gross - fees) * s['quote_usd']
    if net <= 0:
        return None, 'exit_cost_exceeds_proceeds'
    return {'proceeds_usd': net, 'exit_price_usd': net / qty,
            'exit_price_sol': net / s['quote_usd'] / qty,
            'exit_quote_usd': s['quote_usd'], 'exit_fees_usd': fees * s['quote_usd']}, None
