# Entry-frequency comparison — 11 September 2026

This is an in-sample replay of the same 4,456 saved live Pump index observations.
It checks whether the revised rules produce more paper entries on the observed
sample. It is not a forward validation or an exhaustive market backtest.

| Rule set | Purchases | Closed trades | Open positions | Total modeled P&L |
|---|---:|---:|---:|---:|
| baseline | 9 | 8 | 1 | $-16.76 |
| frequent | 21 | 21 | 0 | $-4.37 |

Total P&L includes modeled exit value of open positions, failed-attempt fees and
operating-cost assumptions. The original ledger has one open position marked at
its final observation; it has not been silently treated as closed.

Both rule sets lost money in this short observed sample. The revised rules increase
trading frequency; they have not established a profitable strategy. Their parameters
were selected after observing the original run, so improvement here is in-sample.
That separate experiment at `data/frequent.sqlite` is now stopped and preserved.
The current full-bankroll experiment is `data/cadence.sqlite`; see `CADENCE_COMPARISON.md`.

Replay cannot evaluate the discovery rotation improvement: it only has the tokens
watched by the original collector. The new live collector additionally rotates
stale, exhausted and cooldown tokens to give new candidates room.

Inputs: `exports/baseline-20260910/live.recording.jsonl`.
Configurations: `paper.config.json` and `paper.frequent.config.json`.
Detailed outputs: `exports/frequency-comparison/`.
