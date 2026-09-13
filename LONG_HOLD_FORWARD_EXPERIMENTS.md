# One-day long-hold forward cohorts

These paper-only cohorts start together from a clean ledger on 11 September
2026. They use the same live Pump observations, current ownership gate and
execution-cost assumptions as the existing long-hold ladder. Every cohort has
an independent $100 virtual balance; none is included in the unified account.

| Cohort | Entry band | Observation / activity gate | Capacity | Partial-sale targets |
|---|---|---|---:|---|
| Early frequency | $8k–$15k | 30 sec, 2 changes, +0.5%, $5 reserve growth | 5 | $25k / $50k / $80k |
| Core frequency | $10k–$25k | 30 sec, 2 changes, +1%, $10 reserve growth | 5 | $30k / $60k / $100k |
| Mid-cap patient | $25k–$50k | 45 sec, 3 changes, +1.5%, $20 reserve growth | 4 | $60k / $100k / $160k |

All three retain the 30% modeled stop. The first two use a 35% post-partial-sale
trailing exit; the mid-cap cohort uses 50%. The current $10k–$12.5k ladder
continues as the control and stays in the unified account.

The service evaluates every cohort on each five-second collector batch. An entry
still needs a fresh passed ownership screen, sufficient liquidity and a viable
modeled round trip. This means the wider bands increase *opportunity coverage*,
not a guarantee of more filled paper trades.

Monitor the cards at http://127.0.0.1:8765 or inspect:

```bash
curl http://127.0.0.1:8765/api/long-hold-experiments
```

At the end of the forward window, compare net P&L, completed/open positions,
trade count, win rate, profit factor and peak drawdown. Do not choose a winner
on P&L alone if it has only one or two exits, and do not alter a cohort's JSON
configuration in place: each ledger stores its configuration as an immutable
contract.
