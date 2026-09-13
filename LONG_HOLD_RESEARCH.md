# Early entries and partial exits

The recorded sample supports testing partial exits prospectively. It does **not**
establish a profitable strategy: only 2.41 hours were available at the research
cutoff, and many coin paths are incomplete.

The frozen data ends at **11 September 2026, 02:18 Europe/Belgrade**. It contains
374 remembered coins across the three actual live-data experiments. Synthetic
demonstrations and alternate replay portfolios were excluded.

## What happened after observations near $10k

For each coin, take its first usable recorded Pump quote between $10,000 and
$12,500. This produces a cohort of 100 coins. The reference is an observation,
not a simulated purchase or proof that we could have entered there.

| Subsequent observed valuation | Coins observed reaching it | Median time among timed curve touches |
|---|---:|---:|
| $30k | 14 | 5.1 minutes |
| $60k | 0 | Insufficient observations |
| $100k | 0 | Insufficient observations |

59 coins later had a usable curve quote at least 30% below their reference
valuation. Of the 14 observed $30k touches, 13 preceded the first observed 30%
valuation drop, when such a drop was recorded. These categories can overlap.
53 coins have fewer than five minutes between their reference and their last
subsequent usable curve quote.

**The untouched targets are not failed trades.** Discovery and watch capacity
are bounded, the original strategy mainly watched $10k–$50k, and later follow-up
is incomplete. A coin can leave our quote coverage before its next move. The new
ladder keeps its held coins in the collector's priority set beyond the entry band.

Examples from the recorded cohort:

| Coin | Reference valuation | Highest later observed valuation | Ownership at research cutoff |
|---|---:|---:|---|
| GOTHAM (`3Dng…Kpump`) | $11,152 | $45,505 | Not checked |
| SOLCAT (`8KT7…Vpump`) | $10,804 | $39,967 | Unverified |
| Gösta (`kPxQ…2rpump`) | $10,179 | $34,518 | Flagged |

These illustrate price paths, not recommended purchases. The full addresses and
per-coin paths are in `exports/ladder-research-20260911/coin-paths.csv`. Gösta also
illustrates why a rising valuation must not override an ownership flag.

Across all remembered coins, the last stored ownership results were 21 flagged,
2 clear and 12 unknown; most coins had no result. These are stored results at the
snapshot, not a count of currently valid clear screens. Checks expire after two
minutes. We cannot backdate today's ownership result to an earlier entry.

## Actual paper results at the same cutoff

| Experiment | Closed trades | Open positions | Net portfolio P&L |
|---|---:|---:|---:|
| Original | 8 | 1 | −$16.76 |
| Frequent | 5 | 0 | −$3.71 |
| Cadence | 21 | 0 | −$16.58 |

Each started with its own $1,000 virtual balance. Results are as of each ledger's
last observation, including marked open positions; the earlier experiments are
stopped. They are not one combined wallet return. The earlier six-hour longer-hold
test had no entries and its $100 remained unspent. Its original state and code are
archived, with its state also retained in `coin-memory.sqlite`.

## New prospective ladder

`ladder.config.json` starts a separate $100 virtual test in `data/ladder.sqlite`.
Each entry costs $10 all-in, with at most three open or pending positions and one
attempt per coin. This test does not spend or reset the cadence portfolio.

| Action | Market-cap estimate | Fraction of original tokens |
|---|---:|---:|
| Enter after screens pass | $10k–$12.5k | Buy $10 all-in |
| First partial exit | $30k or higher | 40% |
| Second partial exit | $60k or higher | 30% |
| Third partial exit | $100k or higher | 20% |
| Keep for a longer move | After partial sales | Remaining 10% |

The user's “60kk” is interpreted as **$60k**. These are explicit experimental
settings, not thresholds optimized or validated by the short sample. At the top
of the entry band, the first 40% sale may not recover the entire entry cost after
fees. Market cap can also move with SOL/USD and supply/valuation assumptions;
net proceeds come from the later observed reserve quote, not a cap multiplier.

Entry requires a fresh supported SOL bonding curve, 60 seconds of observed market
history, six samples, at least three distinct reserve-implied price changes,
at least 1% reserve-implied price growth, $10 of real SOL reserve growth, no more
than 35% below the remembered peak, adequate liquidity, and a current matching
ownership screen. FX movement alone cannot satisfy the activity tests. This is
an early-entry hypothesis; the separate six-hour growth classification remains
visible in coin memory and is not the entry gate for this new test.

A target schedules a sale. A subsequent quote at least two seconds later must
still support the target. Its actual modeled net price determines the proceeds.
A jump across multiple targets can combine the triggered fractions into one
sale. Failed exits retain quantity and cost; repeated quotes cannot fill an order.
Every partial exit allocates the original all-in cost pro rata, so realized and
unrealized P&L reconcile without counting fees twice. Position counts decrease
only after the final tokens have sold.

A 30% modeled loss on the remaining cost basis schedules a full remaining exit.
After the first partial sale, a 35% fall from the highest observed token price
also schedules an exit. An ownership flag or a ten-day hold limit can exit sooner
than any target. Targets do not require waiting one day: a fast rally can produce
partial sales within minutes.

Unusable quotes mark the unsold position at $0 without fabricating a sale. A gap
of ten minutes schedules an exit, which still requires a later usable quote.
Temporary graduation gaps can therefore resolve without automatically disposing
of the whole position. Moving to PumpSwap requires an observed completed curve
and a supported quote for the same mint; once selected, the AMM pool is pinned.
Other venues, missing reserves, non-FDV AMM valuations and insufficient indexed
liquidity cannot fill an exit.

The execution model assumes 2.5% fees and 1% adverse fill on each side, $0.10 fixed
cost per attempt, $0.35 unrecovered entry rent, and 5% deterministic failed-attempt
stress. These are frozen sensitivity assumptions, not current measured fees or
transaction simulations. Curve exits account for the modeled remaining holding
in reserve arithmetic. AMM exits use unshifted published reserve estimates; we
cannot reproduce a counterfactual migration or other traders' reactions.

## Reproduce and inspect

The read-only research script produces `analysis.json` and `coin-paths.csv` from
the frozen SQLite snapshots:

```bash
python3 analyze_long_hold.py exports/ladder-research-20260911
python3 -m unittest discover -s tests -q
```

Target touches combine usable timestamped Pump quotes and strictly later
five-minute memory bars. Same-bucket highs are excluded because they may predate
the reference. Bar highs have uncertain within-bar timing and may use another
provider's valuation basis. This path analysis does not claim hypothetical fills,
reconstruct missing ownership, or infer strategy P&L from the observed highs.

The new ladder stores its complete prospective input batches, configuration,
ownership-policy hash, code hash, events, partial sales, completed positions and
equity in its own database. Tests replay recorded inputs and verify the same
state and sale ledger, including after partial exits and restarts.
The dashboard exposes `/api/ladder` and the complete sale CSV at `/api/ladder.csv`.
Its ladder values are all-time and separate from the scalp portfolio period tabs.

Pump's documentation describes the transition from its virtual-reserve curve to
PumpSwap. [Pump program documentation](https://github.com/pump-fun/pump-public-docs/blob/main/docs/PUMP_PROGRAM_README.md)
DEX Screener supplies indexed pair valuations, price and liquidity fields used
for follow-up estimates. [DEX Screener API reference](https://docs.dexscreener.com/api/reference)
