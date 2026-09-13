# Long-hold configuration comparison — 11 September 2026

The active longer-hold ladder is profitable on the current forward paper sample;
the short-term cadence strategy is not. This is an observation from a very small
sample, not a recommendation to trade or a validated edge.

At 17:03 Europe/Belgrade, the two active ledgers contained:

| Strategy | Completed positions | Winners / losers | Modeled trading P&L |
|---|---:|---:|---:|
| Short-term cadence | 65 | 17 / 48 | -$63.43 |
| Long-hold ladder | 5 | 3 / 2 | +$24.39 |

The short-term profit factor was 0.36 (gross wins $35.15 / gross losses $98.58).
The long-hold ladder's corresponding figure was 2.55 ($40.33 / $15.84). Both
figures are too small and too dependent on the observed coins to estimate a
reliable future return.

## Same-input counterfactuals

`compare_long_hold.py` replays every recorded long-hold input batch available at
the time it runs, including the historical ownership evidence and modeled
execution rules. It uses the original $100 ladder budget and reports trading
P&L, excluding the later internal account-allocation transfer. In this snapshot,
the active configuration reproduced exactly: six entries, five completed
positions, and +$24.39 net P&L.

| Preset | Entries | Completed / open | Net modeled P&L | Reading |
|---|---:|---:|---:|---|
| Trailing drawdown 50% | 6 | 5 / 0 | +$31.10 | Best in this sample, but one extra held winner drives it. |
| Delayed targets: $40k / $80k / $120k | 6 | 5 / 0 | +$27.63 | Better in-sample, with materially more exposure to a reversal. |
| Front-loaded 60% / 25% / 10% at $30k / $60k / $100k | 6 | 5 / 0 | +$25.18 | Essentially tied with active; does not justify a change. |
| **Active: $30k / $60k / $100k; 40% / 30% / 20%; 35% trail** | **6** | **5 / 0** | **+$24.39** | Current forward-paper configuration. |
| Earlier targets: $25k / $50k / $80k | 6 | 5 / 0 | +$17.49 | Worse: winners gave up too much upside. |
| Trailing drawdown 25% | 6 | 5 / 0 | +$15.64 | Worse: it sold a winner too early. |
| Stop loss 20% | 6 | 5 / 0 | -$3.39 | Not viable here: it stopped out a later winner. |
| Entry band $10k–$15k | 7 | 6 / 1 | -$8.48 | Worse and leaves an underwater open mark. |
| Entry band $10k–$11k | 0 | 0 / 0 | $0.00 | Too narrow for this sample. |

Increasing the entry activity gate from 1%/$10 reserve growth to 2%/$20 or
3%/$30 made no difference: the same six entries passed. A 40% stop also made no
difference because the two losses had already crossed the existing 30% stop.

## Decision

Keep the active long-hold ladder unchanged for now. The seemingly superior 50%
trailing stop and delayed-target options were selected after seeing five completed
positions, so promoting either would be curve fitting. Continue to collect an
independent forward sample and re-run:

```bash
python3 compare_long_hold.py --output exports/long-hold-config-comparison.json
```

Consider a new, isolated paper ledger only after a pre-committed rule and a
meaningfully larger number of completed long-hold positions. Do not edit
`ladder.config.json` while `data/ladder.sqlite` is active: its contract is
intentionally immutable so the result remains auditable.
