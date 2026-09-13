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
real-trading mode. The current faster experiment is stored in `data/cadence.sqlite`.
Stopping preserves open positions; it does not invent closing trades. Starting the
same command resumes its ledger and pending exits. A long gap restarts observation
windows and marks unavailable exit quotes conservatively.

The dashboard opens on a portfolio brief, current value, period P&L, win rate and
trade activity. It updates every three seconds. Open positions appear newest first;
the closed-trade journal supports winners/losers, token or address search, sorting,
trade details and pagination through the complete history. Ranked winners and
losers aggregate closed-trade results by token. The activity timeline shows actual
ledger events, and feed status is distinct from a pause on new entries.

**Live / Today / 7 days / 30 days** filter performance, charts, closed trades and
activity. Live covers the current experiment; Today starts at midnight in
Europe/Belgrade, and 7/30 days are rolling windows. Current positions and cash
always remain visible. Period P&L is the change in recorded equity from the last
observation before the window (or the initial balance if none exists). Closed-trade
P&L counts whole trades closed in the window; these can differ when positions cross
its boundary. The previous strategy's daily loss halt used **UTC midnight**; the current experiment disables portfolio loss pauses.

Longer windows display only available history, explicitly marked as partial, and
empty windows have no invented return or chart points. The chart samples actual
observations while preserving extrema and endpoints; hovering reveals timestamps
and values. CSV export contains all closed trades. `/api/dashboard` reads the full
ledger and accepts `period`, `outcome`, `sort`, `search` and `page`; the legacy
`/api/state` retains its old report limits for compatibility.

## Portfolio brief and optional AI

`dashboard.config.json` configures the summary service. Its current provider is
OpenAI `gpt-4.1-mini`, using the Responses API and `OPENAI_API_KEY` from the process
environment. No key is sent to the browser or stored in project files. A connected
model curates a headline and the most useful facts from verified portfolio data;
the application supplies the wording and numbers. AI cannot alter the trading
rules, place orders or invent portfolio claims. Current pause, stale-feed and
impaired-position information takes priority over a cached selection.

Successful results are labelled **AI portfolio brief / AI CURATED**. While waiting,
or if the API is unavailable, the panel says **Automatic portfolio brief / DATA
SUMMARY** and explains the connection state. Both update their numbers from the
ledger. The AI generation time is shown separately from the latest observation.
Only aggregate facts and public token labels are sent, with API response storage
disabled. This is fact selection rather than unconstrained model-written analysis.

Requests run asynchronously, at most once every five minutes and only when facts
change. Curation is cached by performance period, so switching periods can show
the automatic brief until its next eligible refresh. Limits are 96 requests and
**$0.25 of conservatively estimated API usage per UTC day**, persisted alongside
the database in `data/cadence.briefs.json`. The estimate reserves input bytes and
maximum output tokens using the configured rates; it is an application guard, not
a provider billing guarantee. Changing model pricing requires updating those
rates. Quota/authentication errors stop retries until the process restarts.

API charges are real and **separate from the virtual portfolio P&L**; usage appears
under Experiment settings. To use the automatic brief with no model calls, set
`summary_provider` to `disabled`. Optional local Ollama support uses `ollama` and a
locally installed model name at `127.0.0.1:11434`. Restart the collector after changing
summary settings, preserving its database and trading configuration.

## Current experiment

`paper.cadence.config.json` contains the **active live-simulation settings**.
`paper.frequent.config.json` retains the previous frequent preset, and
`paper.config.json` retains the original baseline for comparisons and tests. The older
`strategy.paper.json` and `policy.example.json` are research proposals, not its
runtime configuration. The settings are experimental, not optimized for profit.

| Setting | Default |
|---|---|
| Virtual starting balance | $1,000 |
| All-in cost per paper entry | $10 |
| Position / acquisition exposure limits | 6 positions / $60, including pending entries |
| Buy attempt cadence | At most 3 in any rolling 60 seconds; failed attempts count |
| Entry market cap | $10,000–$50,000, using provider quoted total-supply valuation |
| Eligible route | SOL-quoted, incomplete Pump bonding curves; Mayhem excluded |
| Observation | At least 15 seconds and 4 samples; gaps over 20 seconds reset history |
| Long momentum | 0.75%–25% reserve-implied SOL price rise over approximately 15 seconds |
| Short momentum | At least 0.2% over approximately 5 seconds |
| Reserve increase | At least $10 in real SOL reserves during the short window |
| Exit triggers | +20% net executable return, −10%, missing usable quote, or 3-minute hold |
| Order delay | At least 2 seconds and a subsequent observation; typically 5 seconds with polling |
| Pool/route fee assumption | 1.25% + 0.50% on each trade |
| Adverse fill assumption | 0.50% on each side, in addition to pool arithmetic |
| Fixed costs | 0.00015 SOL per attempt; 0.0021 SOL entry account-rent allowance |
| Failure stress | 5% deterministic pseudorandom failed attempts; configurable, not measured |
| Liquidity constraints | At least $1,000 real reserves; entry ≤1% of real SOL reserves; impact ≤3% |
| Round-trip cost filter | Skip if immediate modeled round-trip loss exceeds 8% |
| Portfolio loss pauses | Disabled: the user authorized running against the full virtual bankroll |
| Success target | $50 net portfolio profit, confirmed after all positions exit |
| Bankroll stop | Less than $10 cash with no remaining open positions |

There are at most six attempted entries per token per experiment, with a
minimum 45-second cooldown after closing or a failed entry. Every re-entry needs
a fresh qualifying observation window. A held or exit-pending position cannot be
bought again. There is no averaging down or automatic compounding. A failed entry
consumes an attempt. A failed exit retains
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
the true rate. The paper ledger's daily operating cost defaults to $0. Allocate
actual data, machine, hosting or research costs in a separate experiment when
evaluating profitability. The optional dashboard AI has separate real API usage;
its charges are not subtracted from this existing paper experiment. Buys and sells
always use fictional money.

## Data coverage and profitability limits

The collector polls the public Pump index approximately every five seconds. Every
five seconds it samples up to 70 recently created and 70 recently active incomplete
coins, then follows at most 24 candidates plus any positions needing exits. Coin
details and SOL/USD are read with bounded concurrent GET requests. Coins in cooldown or
at their attempt limit leave the watchlist; stale coins are removed after 45
seconds of source age and out-of-range coins after 90 seconds. Eligible coins in
the $10k–$50k band get priority. Positions and pending orders always retain their
watch slots. HTTP rate-limit
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

## Frequency update — 11 September 2026

The first run was spending most of its watch slots on stale tokens or tokens it
had already attempted once. That version rotated those slots, allows
bounded re-entries, shortens observation to 30 seconds, and refreshes discovery
every 10 seconds. Trade size remains $20, at most three simultaneous positions
and $60 acquisition exposure; fees, exit triggers, liquidity checks and loss halts
remain in force. The change increases eligibility, not a promised trade rate.

The original ledger remains at `data/live.sqlite`; its frozen source and settings
are in `experiments/baseline-20260910/`, and its exports are in
`exports/baseline-20260910/`. It is a stopped experiment and any position still open
there remains marked as of its last observation. The new experiment starts with a
separate virtual $1,000 balance, so earlier losses are preserved in the baseline,
not folded into the new run's P&L. See `FREQUENCY_COMPARISON.md` for the same-data
replay comparison; this short in-sample check does not prove profitability.

To stop the current collector gracefully from another terminal:

```bash
python3 stop_collector.py --db data/cadence.sqlite
```

## Full-bankroll cadence run — 11 September 2026

The user requested more buys per minute, then authorized continuing until the
simulation is profitable or its trading bankroll is depleted. The active preset
uses $10 entries, six slots, 15-second observations, a 45-second re-entry cooldown,
and a three-minute maximum hold. The cap is **three entry attempts per rolling
minute**, enforced both when scheduling and filling, persisted across restarts.
It is a ceiling, not a quota: qualifying observations, cash, liquidity and position
capacity still determine actual purchases. Exits do not consume the buy budget.

Daily-loss and peak-drawdown pauses are disabled by setting their fractions to 0.
At $50 net portfolio profit, the engine cancels pending buys and schedules delayed
exits. It confirms success only when all positions have closed and virtual cash
is at least $1,050. If exit fills miss the target, entries can resume. Otherwise it
continues until cash cannot fund a $10 order and all positions are closed. A small
cash remainder is reported as-is; it is not rounded to zero. Missing quotes keep
positions pending, so neither outcome is guaranteed to occur within a fixed time.
The collector remains available to serve the completed result after trading stops.

`profit_target_usd` selects the target (0 keeps trading through profits), and
`stop_when_unfunded: 1` enables the depleted-bankroll terminal state. Changing any
experiment setting requires a new ledger. The active ledger started with a fresh
virtual $1,000; earlier losses remain in `data/frequent.sqlite`, with reports in
`exports/frequent-before-cadence-20260911/` and frozen code/settings in
`experiments/frequent-before-cadence-20260911/`.

The same 48.53-minute recording produced 5 purchases and −$3.71 modeled P&L with
the previous preset, versus 73 purchases and −$81.43 with the new preset, including
four remaining positions. The old run stopped entries on its daily loss rule;
the new one did not. This comparison changes both cadence and loss policy. It
shows more activity, not a profitable strategy, and cannot evaluate newly watched
tokens or the discovery change. See `CADENCE_COMPARISON.md` and
`exports/cadence-comparison-20260911/` for the exact inputs and outputs.

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
python3 paper_trader.py export --db data/cadence.sqlite
python3 paper_trader.py replay --input exports/cadence.recording.jsonl --db data/replay-original.sqlite
python3 paper_trader.py report --db data/replay-original.sqlite
```

Replay consumes observations in their original arrival order and defaults to the
configuration saved in the recording. No look-ahead candle fills are used. The same
inputs and implementation produce the same decisions, modeled failures and ledger.
The recording header stores a configuration and an implementation hash.

To test a different budget, delay or rule, copy `paper.cadence.config.json`, edit that copy,
and use a **new** database. A separate run is mandatory when configuration, source
or implementation changes; existing experiments cannot be silently rewritten.

```bash
python3 paper_trader.py replay --input exports/cadence.recording.jsonl --config research-variant.json --db data/variant.sqlite
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
Dashboard tests cover local day boundaries and DST, period accounting, complete
history, sorting, pagination, verified brief content and persistent AI limits.
An optional Playwright browser check is in `tests/check_dashboard.py`; it checks
period/outcome filters, search, sorting, trade details, charts and responsive layouts.
It is a development check, not a runtime dependency:

```bash
timeout 40 python3 tests/check_dashboard.py
```

Desktop/mobile screenshots are written to `exports/dashboard-desktop.png` and
`exports/dashboard-mobile.png`. The previous UI is archived in
`experiments/ui-before-20260911/web/`. This redesign resumes the same ledger and
does not change the simulated strategy or reset its loss halts.

## Sources

- [Pump fees](https://pump.fun/docs/fees): bonding curve and graduated-pool fee schedules.
- [Pump program documentation](https://github.com/pump-fun/pump-public-docs/blob/main/docs/PUMP_PROGRAM_README.md): virtual and real reserves and curve completion.
- [PumpPortal fees](https://pumpportal.fun/fees/): optional route-cost assumption; this app does not call its transaction APIs.
- [PumpPortal data documentation](https://pumpportal.fun/data-api/real-time/): metered trade feeds would be a later data upgrade, not part of this free collector.
- [OpenAI GPT-4.1 mini](https://developers.openai.com/api/docs/models/gpt-4.1-mini): model and reference rates used by the optional summary service.
- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs): schema-constrained fact selection through the Responses API.

`DESIGN.md` describes the broader research architecture. This README describes the
implemented paper baseline and its actual limits.
