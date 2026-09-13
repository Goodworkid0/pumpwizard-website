# Pump Paper

A working **off-chain paper trader** for testing short-term Pump.fun strategies.
It collects real public market observations and simulates buys and sells using
virtual money. It has no wallet connection, signing code, transaction submission,
or paid data subscription. Python 3.11+ and its standard library are sufficient.

## Run

From this directory:

```bash
python3 paper_trader.py live
```

Open **http://127.0.0.1:8765**. The process continues until Ctrl+C or SIGTERM.
The computer and process must remain running to collect observations. The dashboard
shows when its collector is stale or disconnected. Nothing runs on a remote server.

The word `live` means **live market data with fictional trades**. There is no
real-trading mode. The default experiment is stored in `data/live.sqlite`.
Stopping preserves open positions; it does not invent closing trades. Starting the
same command resumes its ledger and pending exits. A long gap restarts observation
windows and marks unavailable exit quotes conservatively.

The dashboard shows virtual cash and equity, entry/exit prices, realized and
unrealized P&L, win rate, fees, failed attempts, drawdown, candidate decision reasons,
and results by entry market-cap band. Its equity chart supports hover inspection.
CSV downloads contain the complete closed-trade journal; the screen shows the most
recent 500 closed trades and 2,000 equity observations. SQLite retains the full run.

## Current experiment

`paper.config.json` contains the **active simulator settings**. The older
`strategy.paper.json` and `policy.example.json` are research proposals, not its
runtime configuration. The settings are experimental, not optimized for profit.

| Setting | Default |
|---|---|
| Virtual starting balance | $1,000 |
| All-in cost per paper entry | $20 |
| Position / acquisition exposure limits | 3 positions / $60, including pending entries |
| Entry market cap | $10,000–$50,000, using provider quoted total-supply valuation |
| Eligible route | SOL-quoted, incomplete Pump bonding curves; Mayhem excluded |
| Observation | At least 60 seconds and 7 samples; gaps over 20 seconds reset history |
| Long momentum | 3%–25% reserve-implied SOL price rise over approximately 60 seconds |
| Short momentum | At least 0.5% over approximately 15 seconds |
| Reserve increase | At least $30 in real SOL reserves during the short window |
| Exit triggers | +20% net executable return, −10%, missing usable quote, or 10-minute hold |
| Order delay | At least 2 seconds and a subsequent observation; typically 5 seconds with polling |
| Pool/route fee assumption | 1.25% + 0.50% on each trade |
| Adverse fill assumption | 0.50% on each side, in addition to pool arithmetic |
| Fixed costs | 0.00015 SOL per attempt; 0.0021 SOL entry account-rent allowance |
| Failure stress | 5% deterministic pseudorandom failed attempts; configurable, not measured |
| Liquidity constraints | At least $1,000 real reserves; entry ≤1% of real SOL reserves; impact ≤3% |
| Round-trip cost filter | Skip if immediate modeled round-trip loss exceeds 8% |
| Entry halts | 2% of starting capital lost during a UTC day, or 10% peak drawdown |

There is one attempted entry per token per experiment, no averaging down, and no
automatic compounding. A failed entry consumes its attempt. A failed exit retains
the position and retries on a later observation. Halts block new entries while
preserving exit attempts. None of these triggers guarantees a maximum loss or fill.
The market-cap band is checked at entry execution and does not prevent exits.

The rule uses **price and reserve momentum**, not distinct-buyer counts. Increasing
reserves are an imperfect activity proxy; they cannot identify buyer quality,
wash trading, linked wallets, holder concentration, or creator selling. Mint
authorities and Token-2022 extensions are not validated by this snapshot feed.
This is a deliberately limited research baseline, not a completed token-risk system.

## How fictional fills and accounting work

1. Record each token snapshot's local arrival, provider update time and SOL/USD
   timestamp. Stale, regressing, malformed or unsupported data cannot produce fills.
2. A qualifying observation schedules an entry. Wait for the configured delay
   and a later observation. Recheck range, costs, liquidity, budget and exposure.
3. Use virtual reserves `x` and `y` for an approximate constant-product quote at
   the exact virtual order size. Apply configured fees, adverse fills and fixed costs.
4. Maintain an approximate counterfactual reserve offset for our own token position.
   At each external snapshot, the shadow curve is `x − q, x*y/(x − q)` for held
   quantity `q`, with the corresponding real-SOL reserve adjustment. This accounts
   for the modeled purchase in its subsequent exit. It cannot predict how other
   traders would react to a real order, competition, MEV or changed pool behavior.
5. Monitor full-position exit proceeds after costs. Schedule exits on a trigger,
   then price them using a later usable snapshot. A price gap can breach the stop.
   Missing quotes and migrations leave an **open exit-pending position**, valued
   at $0 until a supported quote returns. The implementation does not route into
   PumpSwap or silently remove migrated positions from results.

`Total P&L = cash + modeled exit value of open positions − initial balance`.
Realized net P&L includes closed-trade proceeds minus acquisition costs, less
failed-attempt fees and operating costs. Unrealized P&L includes the entire remaining
acquisition cost. These reconcile to total P&L. Fees already embedded in fills are
not deducted again. Rent is treated as an unrecovered expense, conservatively.
Adverse fills and pool impact affect fill prices; the fee counter contains explicit
fees, not another deduction for these effects.

This is a **USD-denominated virtual-cash experiment**. Entry and exit values use
the observed SOL/USD rate, while uninvested cash stays in USD. It does not reproduce
the FX P&L of an actual wallet holding SOL. Per-token SOL and USD fill prices and
conversion rates are recorded so this distinction stays visible.

The fixed fee values are assumptions, not fees fetched from current contract
configuration. A 5% failure probability is a stress assumption, not a claim about
the true rate. Daily operating cost defaults to $0 because this version uses free
public requests and no model service; change it to include your actual allocated
data, machine, hosting or research costs. No money is spent by the simulation.

## Data coverage and profitability limits

The collector polls the public Pump index approximately every five seconds. Every
20 seconds it samples up to 70 recently created and 70 recently active incomplete
coins, then follows at most 18 candidates plus any positions needing exits. Coin
details and SOL/USD are read with bounded concurrent GET requests. HTTP rate-limit
responses cause backoff. No authentication or paid trade stream is configured.

These are undocumented public endpoints, verified to respond on 10 September 2026;
availability and field definitions can change. Provider timestamps help detect
staleness but do not prove a fresh on-chain state. This bounded discovery sample can
miss launches and rapid price moves. Only watched observations are retained in full;
the discovery count is a collector-session count, not an exhaustive market census.

Therefore, results are **snapshot-based estimates**, not evidence of executable
profitability. There is no chain-level instruction simulation, transaction quote
verification, holder-risk validation, guaranteed feed completeness, or subsecond
execution. Unsupported migrations remain impaired and may understate recoverable
value. The dashboard flags them so they cannot masquerade as completed losses or wins.

Evaluate multiple independent days, include open/impaired positions, examine profit
concentration and net expectancy, and stress costs/delays before judging the strategy.
Changing parameters after seeing results is in-sample research; evaluate chosen
parameters prospectively on newly collected data. A few profitable paper trades
do not establish an edge.

## Demo, recordings, replay and exports

Generate an explicitly synthetic demonstration in its own database:

```bash
python3 paper_trader.py demo --db data/demo-v1.sqlite
python3 paper_trader.py dashboard --db data/demo-v1.sqlite --port 8766
```

The synthetic sequence deliberately exercises a winner, a loser and a stale exit.
It never enters the live-observation database and is not market-performance evidence.

After stopping the collector, export its complete inputs and ledger:

```bash
python3 paper_trader.py export --db data/live.sqlite
python3 paper_trader.py replay --input exports/live.recording.jsonl --db data/replay-original.sqlite
python3 paper_trader.py report --db data/replay-original.sqlite
```

Replay consumes observations in their original arrival order and defaults to the
configuration saved in the recording. No look-ahead candle fills are used. The same
inputs and implementation produce the same decisions, modeled failures and ledger.
The recording header stores a configuration and an implementation hash.

To test a different budget, delay or rule, copy `paper.config.json`, edit that copy,
and use a **new** database. A separate run is mandatory when configuration, source
or implementation changes; existing experiments cannot be silently rewritten.

```bash
python3 paper_trader.py replay --input exports/live.recording.jsonl --config research-variant.json --db data/variant.sqlite
```

The system uses an exclusive database process lock and atomic SQLite transactions.
Do not start another CLI against the active collector's database; use its dashboard
and CSV download while running. The collector exports a JSON report and CSV when
it stops. All observations, decisions, equity points and open/pending state survive
normal restarts. Preserve the original code with its recordings for exact replay.

## Verification

```bash
python3 -m unittest discover -s tests -v
node --check web/app.js
```

Tests cover delayed fills, range rechecks, exits outside the entry band, fees and
ledger reconciliation, quote gaps and migration, failures, loss halts, restarts,
atomic rollback, deduplication, chronological replay and invalid configurations.
An optional Playwright browser check is in `tests/check_dashboard.py`; it is a
development check, not a runtime dependency.

## Sources

- [Pump fees](https://pump.fun/docs/fees): bonding curve and graduated-pool fee schedules.
- [Pump program documentation](https://github.com/pump-fun/pump-public-docs/blob/main/docs/PUMP_PROGRAM_README.md): virtual and real reserves and curve completion.
- [PumpPortal fees](https://pumpportal.fun/fees/): optional route-cost assumption; this app does not call its transaction APIs.
- [PumpPortal data documentation](https://pumpportal.fun/data-api/real-time/): metered trade feeds would be a later data upgrade, not part of this free collector.

`DESIGN.md` describes the broader research architecture. This README describes the
implemented paper baseline and its actual limits.
