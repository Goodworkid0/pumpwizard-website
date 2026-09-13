"""Deterministic, versioned experiment definitions; catalog entries are not funding."""
import copy
import hashlib
import json

FAMILIES = {
    'persistent': ('Persistent leader', 'Positive progress through both halves of the window, with price holding near its high.'),
    'higher-low': ('Higher-low trend', 'Three successive pullback lows rise, followed by a recovery.'),
    'multi-window': ('Multiple-window confirmation', 'Full-window, half-window and recent-quarter returns all confirm growth.'),
    'squeeze': ('Volatility squeeze breakout', 'A quiet middle range breaks upward with growing liquidity.'),
    'retest': ('Breakout retest', 'A prior range breaks, retests its high and then recovers.'),
    'post-grad': ('Post-graduation survivor', 'A previously observed completed curve stabilizes on one PumpSwap pool.'),
    'liquidity': ('Liquidity-backed growth', 'Price and observable liquidity strengthen together.'),
    'relative': ('Relative-strength leader', 'Top-quartile return among at least four observed peers in the same cap band.'),
    'second-wave': ('Second-wave re-entry', 'A recovery entry followed by later entries only after exit, cooldown and a fresh signal.'),
    'recovery': ('Recovery-base formation', 'A substantial decline forms a stable base and then reclaims.'),
}
BANDS = ((8000,25000),(25000,50000),(50000,100000),(100000,250000),(250000,1000000))
WINDOWS = (300,1800,7200,21600)
HOLD_DAYS = (1,3,7,14,30)


def catalog(base):
    result=[]
    for family,(label,description) in FAMILIES.items():
        for band,(low,high) in enumerate(BANDS):
            for seconds in WINDOWS:
                for days in HOLD_DAYS:
                    cfg=copy.deepcopy(base)
                    cfg.update(entry_style=family,entry_policy='simulation_unverified',budget_usd=500,
                        order_usd=10,max_positions=20,entry_cap_min=low,entry_cap_max=high,
                        observation_seconds=seconds,minimum_samples=6,minimum_price_changes=3,
                        minimum_growth_fraction=.02,minimum_reserve_growth_usd=5,
                        max_sample_gap_seconds=180,quote_max_age_seconds=120,entry_expiry_seconds=180,
                        max_hold_seconds=days*86400,maximum_peak_drawdown=.65,
                        quote_gap_exit_seconds=1800,stop_loss_fraction=-.30,trailing_drawdown_fraction=.35,
                        targets=[{'market_cap_usd':high*factor,'initial_fraction':fraction}
                                 for factor,fraction in ((2,.4),(4,.3),(10,.2))])
                    # The same paired observation/holding profiles appear in every
                    # family and cap band; activation is preselected, never by P&L.
                    pilot=(seconds,days) in ((300,7),(1800,30))
                    ident=f'r1-{family}-b{band}-{seconds}s-{days}d'
                    result.append({'id':ident,'label':f'{label} · ${low//1000}k–${high//1000}k · {seconds//60}m/{days}d',
                        'family':family,'description':description,'config':cfg,'pilot':pilot,
                        'config_hash':hashlib.sha256(json.dumps(cfg,sort_keys=True).encode()).hexdigest()})
    return result
