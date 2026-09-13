# Forward experiment matrix

The rollout adds **100 funded simulations** to the 39 existing strategies without
resetting their ledgers. Each new strategy starts with an independent $500 and
models $10 entries, up to 20 concurrent positions. No live orders are possible.

## Catalog and pilot

`paper/experiment_catalog.py` deterministically generates 1,000 configurations:

- Ten families: persistent leader, higher-low trend, multi-window confirmation,
  squeeze breakout, breakout retest, post-graduation survivor, liquidity growth,
  relative strength, second-wave re-entry, and recovery-base formation.
- Five entry cap bands: $8k–$25k, $25k–$50k, $50k–$100k, $100k–$250k, $250k–$1M.
- Four observation windows: 5 minutes, 30 minutes, 2 hours, 6 hours.
- Five maximum holding horizons: 1, 3, 7, 14, 30 days.

The preselected pilot runs **5m observation / 7d maximum hold** and **30m / 30d**
for every family and band: 10 × 5 × 2 = 100. These paired profiles do not isolate
observation length from holding length. Later factorial cohorts are needed to
measure those effects separately. Maximum hold is a ceiling, not a minimum:
stop losses, missing-quote exit orders, targets and trailing exits can end trades
earlier. A missing quote marks the unsold position at zero; it is not a fake sale.

The remaining 900 catalog profiles are staged and unfunded. `research_experiments.json`
selects `pilot-100`; `all-1000` is an explicit later activation, not an automatic
profit-based promotion. Existing funded configurations cannot be edited in place.
Changed experiments need a new version/identity and a separate ledger.

## Collection and execution

One shared observation archive and feature calculation serve all new cohorts.
Each cohort has a separate execution ledger and cash balance. Each transition
references the immutable shared input by SHA-256; signal/fill records also carry
the code hash and feature values. Shared inputs include the exact feature data
needed for replay. Checkpoints restore live windows after restart without
inventing historical fills. Historical recordings are not fed into live trading.

Curve inputs come from the existing public Pump index collector. Completed-curve
discovery and batched indexed PumpSwap quotes also support entries above
graduation. These are observed snapshots, not an exhaustive on-chain trade feed
or an executable price guarantee. Unknown upstream quote timestamps stay marked
unknown. Unsupported pools, changed pools, stale quotes and observation gaps
cannot complete an entry window. Quote age is capped at 120 seconds and gaps at
180 seconds. Simulations still include delayed fills, liquidity checks, fees,
adverse fills and failed-attempt stress.

New unverified research cohorts retain ownership evidence but do not let RPC
ownership results prevent entries or force exits. Strict-policy execution retains
ownership gates. Existing 39 strategy rules are unchanged. Second-wave cohorts
alone can re-enter after a completed exit, at least one hour's cooldown and an
entire fresh observation window.

The follow-up worker has a 30-token request budget. It reserves slots for due
outcomes and held AMM positions, maintains a stable cap-balanced panel (up to
four tokens per band), and uses remaining slots for discovery/rotation. The
collector retains healthy curve candidates long enough for funded observation
windows. This is bounded coverage: more simulations do not mean more unique
coins or independent evidence. Full market coverage will require a different
collection capacity plan, not one RPC worker per strategy.

## Measurement and comparison

- Research markers retain entry, partial exit and final exit timestamps.
- Forward horizons: 5m, 15m, 1h, 6h, 24h, 7d, 30d, including after exit.
- On-time observations allow 10% of the horizon in delay, with a 60-second floor
  and one-hour ceiling. Late and missed observations are separate categories.
  A later quote may replace a missed category with a late observation; it never
  becomes on-time. Failures use bounded retry backoff. Nothing is forward-filled.
- Directory metrics use all recorded entries/closed trades, not paginated rows.
  Win rate excludes open trades; partial sales are not extra closed wins.
  New cohorts also report unique coins and maximum marked-equity drawdown.
- Different strategies buying the same coin are correlated experiments. Rank by
  profit only as an exploratory view, not evidence of a reliable winner. Report
  unique coins, completed trades, cohort age, drawdown and coverage alongside P&L.
  Evaluate candidate rules on later, untouched time periods before promoting them.

Run the first-day review for feed/cycle health and coverage, not as a verdict on
7–30 day holdings. Before funding the other 900, verify stable observations per
band/window, quote-age distribution, outcome completeness, API/disk/CPU headroom,
and a separate full-scale synthetic benchmark. No automatic expansion occurs.

## Operations and checks

- `/engine`: search, family/status filters, profit/trade/win-rate sorting,
  24-card pages and linked individual rules/ledgers.
- `/research`: source-attributed dataset and on-time/late/missed coverage.
- `/acp`: inspect all generated configurations; the existing composer creates
  legacy-family cohorts only. ACP is not linked from public navigation.
- `/api/public/history.csv` and `/api/public/research.csv`: recorded histories.
- `python3 -m unittest discover -s tests -p 'test_*.py' -q`
- `python3 scripts/verify_research_pilot.py`: synthetic 100-runner benchmark in a
  temporary directory, never production data.
- `python3 tests/check_research_ui.py`: read-only browser checks of the pilot.
- `python3 scripts/backup_research_rollout.py`: online SQLite backups of the 39
  legacy ledgers and coin memory. Backups are additive, not resets.

Initial synthetic benchmark: 65 shared batches / 6,500 ledger references,
156 entries and 117 closes; median cycle 0.312s, p95 0.346s on this machine.
These numbers test mechanics and throughput, not strategy profitability.
