# Pump Paper

A working **off-chain paper trader** for testing short-term Pump.fun strategies.
It collects real public market observations and simulates buys and sells using
virtual money. It has no wallet connection, signing code, transaction submission,
or paid data subscription. Python 3.11+ and its standard library are sufficient.

## Unified paper account

The active account combines the short-term strategy in `data/cadence.sqlite` and
long-hold ladder in `data/ladder.sqlite`. Original virtual funding totals **$1,100**
($1,000 short-term plus the ladder's earlier $100). The account was allocated
approximately **50/50 using current equity**, preserving earlier gains/losses.
At activation, combined equity was **$1,083.42**, allocated **$541.71 per strategy**;
the existing **$16.58 net loss** remained intact. There is no fresh top-up,
withdrawal, reset or automatic ongoing rebalance.

`data/account.sqlite` records the internal capital transfer. Each strategy can
spend only its own allocation, including cash reserved for pending entries. This
keeps the existing entry sizes, ownership gates and exit rules. Short-term profit
and bankroll stops apply to its own allocation; ladder exits follow its partial
sale plan. Allocation percentages drift as the strategies gain or lose value.

The dashboard combines current cash, reserved/available cash, estimated exit value
of open positions, realized/unrealized P&L, closed trades and all sale fills.
Entry/exit fees are already reflected in cash and P&L and are never deducted a
second time. Partial ladder sales realize only their allocated acquisition cost;
remaining tokens retain their remaining cost. The original funding plus realized
net P&L plus unrealized P&L must equal account equity. Every recorded cash
checkpoint must reconcile to purchases, sales, failed fees and operating costs.
Missing exit quotes are conservative marks, not completed sales.

Live / Today / 7 days / 30 days apply to the unified account. P&L charts and returns
exclude funding and internal transfers, while the equity chart shows actual
account value. The return denominator is opening equity plus funding in the
window (not a time-weighted return). Allocation cards and the balance sheet always
show current balances. Archived original/frequent research portfolios remain
available in the archive summary and coin memory, outside active account capital.

The account statement includes every recorded action except routine candidate
rejections. It supports strategy/action filters, token/reason search, pagination
and `/api/account.csv` for the full history. Cash-after values always use the full
chronological account ledger, even when the statement is filtered.
`/api/account` returns the balance sheet and allocations; `/api/trades.csv` combines
fully closed positions across both strategies. `/api/ladder.csv` remains a detailed
export of every ladder partial/final sale. Provider API charges remain actual
service usage, separate from fictional USD.

For migration/recovery: stop `pump-agent.service`, run `python3 migrate_account.py`
to review balances, then `python3 migrate_account.py --apply`. The migration takes
both writer locks, backs up the native ledgers under `experiments/`, and commits
the two dated cash changes and transfer record in one SQLite rollback-journal
transaction. Re-running does not repeat the transfer. Preserve all three databases
together. Native execution contracts remain unchanged; `paper/funding.py` adds a
hashed funding policy, and recording exports retain the original starting capital
plus dated transfers for deterministic replay. File-based CLI reports/exports
continue to describe the selected strategy; account endpoints describe both.

## Run

The installed background service runs the collector, ownership checks, coin memory
and longer-hold paper test independently of the dashboard. Open
**http://127.0.0.1:8765** whenever you want to view it; closing the browser or terminal
does not stop collection. The same databases and portfolio resume after restarts.

```bash
systemctl --user status pump-agent.service
systemctl --user restart pump-agent.service
systemctl --user stop pump-agent.service
systemctl --user start pump-agent.service
journalctl --user -u pump-agent.service -n 30 --no-pager
```

The service is enabled at startup, with user lingering enabled so it also runs
without a logged-in desktop session. Unexpected process exits restart after ten
seconds. An explicit service stop stays stopped until started again or the next
boot; `systemctl --user disable --now pump-agent.service` also disables autostart.
Logs go to the system journal. Core analysis and paper trading need no browser
requests; the optional paid AI brief is requested only while viewing the dashboard.

This is a service on **this computer**, which must stay powered, awake and connected
to the internet. Shutdown and suspend create real observation gaps. It is not a
cloud deployment, and the dashboard remains accessible only on this machine.

The unit is in `deploy/pump-agent.service`, with paths for this checkout. To
reinstall it here after stopping any manually launched collector:

```bash
systemctl --user enable --now /home/c/Desktop/projekti/pump-agent/deploy/pump-agent.service
loginctl enable-linger "$USER"
```

Optional provider credentials are read from `data/service.env` at startup. This
private file has mode `0600`, is excluded with `data/`, and preserves the configured
`OPENAI_API_KEY` and, when supplied, `SOLANA_RPC_URL` across reboots. Do not include
it in shared exports. Update that file and restart the service to change providers.
The unit and logs contain no credential values.

For a manual foreground run, first stop the service, then from this directory:

```bash
python3 paper_trader.py live
```

The foreground process continues until Ctrl+C or SIGTERM. The dashboard shows
when its collector is stale or disconnected.

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
activity. Live covers both active strategies; Today starts at midnight in
Europe/Belgrade, and 7/30 days are rolling windows. Current positions and cash
always remain visible. Period P&L is the change in cumulative trading P&L from the last
observation before the window; funding and transfers are excluded. Closed-trade
P&L counts whole trades closed in the window; these can differ when positions cross
its boundary. The previous strategy's daily loss halt used **UTC midnight**; the current experiment disables portfolio loss pauses.

Longer windows display only available history, explicitly marked as partial, and
empty windows have no invented return or chart points. The chart samples actual
observations while preserving extrema and endpoints; hovering reveals timestamps
and values. CSV export contains all closed trades. `/api/dashboard` reads the full
ledger and accepts `period`, `outcome`, `sort`, `search` and `page`; the legacy
`/api/state` reports account balances with the underlying short-term report labelled separately.

## Coin memory and ownership gate

The **Coin memory** panel remembers observed tokens across the original, frequent
and current cadence experiments. It records first/current/peak valuation, first
observations above $20k/$50k/$100k and larger milestones, drawdown, changed market
states across five-minute windows, our previous closed trades and ownership flags.
Repeated identical polls do not increase its market-change counter. Our own paper
trades are displayed as separate evidence and do not raise a market-activity score.
The classification is an explicit research rule, not an AI prediction of returns.
Quote changes can include SOL/USD movement; independent provider transaction activity
is additionally required for longer-hold entries. Search queries the complete archive;
the default table displays the most recent 100 classified coins.

Memory is stored in `data/coin-memory.sqlite`, with restart-safe import cursors and
batch deduplication. Only the three configured actual live ledgers are imported;
synthetic demonstrations and alternate replay portfolios are excluded. A bounded
round-robin watch keeps checking remembered coins after they leave $50k or graduate,
using DEX Screener and Pump follow-ups. Held longer-term positions get priority.
DEX valuations prefer FDV for comparison with Pump's total-supply valuations;
provider market cap is used if FDV is absent. These are estimates, and provider
basis/supply differences can affect apparent growth. Data gaps remain visible.

Before **any new paper buy**, `paper/guarded.py` requires a matching, current
ownership result. The gate runs both at the signal and fill checks. Missing,
expired, rate-limited and blocked evidence cannot buy. Existing positions can exit.
The gate's activation time, code hash and policy are saved as `entry_policy` in the
existing ledger and exported with the recording. A replay applies it only after
its original activation time; previous trades, balance and positions are preserved.
A pre-change copy is in `experiments/before-brain-20260911/`.

Ownership checks use Solana RPC to inspect mint/freeze controls, supported token
extensions, the full current token-account set, balances aggregated by owner, and
a bounded sample of recent mint/large-holder transactions. Balances must reconcile
to mint supply for a passed screen. Only program-verified Pump curve and PumpSwap
vaults are excluded; concentration uses distributed supply after those exclusions.
The current rules reject an owner above 10%, ten owners above 35%, large external
transfers (5%), fan-out distribution, circular transfers and suspicious control
instructions. Ordinary mint initialization and authority revocation are not flags.
A screen requires at least five relevant recent transactions and expires after
120 seconds. The sample is at most 12 transactions, not a complete transfer graph.

`ownership.config.json` contains these rules. `SOLANA_RPC_URL` can supply an HTTPS
provider endpoint through the process environment; otherwise the official public
mainnet endpoint is used. Provider URLs and credentials are never sent to the UI.
Rate limits cause backoff and an **Unverified** status. A previous flag remains
blocked through expired or unavailable rechecks until a complete passed screen resolves it. A **Screen passed** result
means no listed flag was found in the available evidence. It does not prove that
wallets are independent or rule out common funding, all transfers, LP control,
creator linkage or malicious behavior. Unknown extensions and incomplete scans
fail closed. Historical check results and evidence hashes are retained in the
ownership audit table; the raw full transaction/account responses are not retained.

## Active long-hold ladder

`ladder.config.json` configures the active longer-hold experiment in its own
`data/ladder.sqlite`. Its $100 virtual budget uses $10 all-in entries around
$10k–$12.5k MC, at most three open/pending positions, 60 seconds of observed
reserve activity, liquidity checks and a current ownership screen.

It sells **40% of original tokens at $30k**, **30% at $60k**, and **20% at $100k**,
leaving **10%** for a longer move. A target schedules a partial sale; the actual
fill uses a later fresh quote that still meets it. Cost basis is allocated pro
rata and every sale records prices, costs, fees and proceeds. Partial sales do
not count as fully closed positions. There is no one-day minimum before taking
profits. A 30% modeled loss, a 35% price drawdown after the first partial sale,
ownership flags, a prolonged quote gap or ten-day limit can exit the remainder.

Held coins remain in the collector's priority set beyond the entry band. An
observed completed curve and a supported same-mint PumpSwap FDV/reserve quote
allow a modeled route transition. Missing quotes keep unsold tokens open with a
$0 mark; they never become fictional sales. This is still an indexed sensitivity
model, not a transaction simulation. Fees are 2.5% plus 1% adverse fill per side,
$0.10 fixed per attempt and $0.35 entry rent, with 5% failed-attempt stress.

The dashboard's **Long-hold ladder** shows milestones, remaining fractions,
realized/unrealized results, entry reasons and partial-sale history.
`/api/ladder` exposes its report and `/api/ladder.csv` exports every sale. These are
all-time results of the separate ladder portfolio; the main period tabs filter
the cadence experiment. Inputs, events, positions and partial-exit progress are
saved atomically and resume under the same background service.

See [LONG_HOLD_RESEARCH.md](LONG_HOLD_RESEARCH.md) for the 2.41-hour evidence
sample, cohort CSV, limitations and complete rules. No past observations are
converted into ladder purchases. Changing its frozen config/code requires a new
ledger. The previous unused $100 experiment and all cadence history are retained;
the pre-change code and database copies are in `experiments/before-ladder-20260911/`.

## Archived six-hour longer-hold test

The following describes the previous longer-hold experiment. It had no entries
when the early ladder replaced it. Its configuration, $100 unspent balance and
original state remain recorded. Its six-hour classification continues as a coin
memory research signal, separate from the early ladder's entry rules.

`brain.config.json` configures a separate **$100 virtual portfolio**, with $10
entries and at most three holdings. This does not spend or reset the cadence
portfolio. Imported historical observations only seed memory: they never create
backdated longer-hold purchases. The test starts making decisions prospectively.

Entry requires at least six hours of history, changed observations in 12 distinct
five-minute windows across at least three hours, at least 50% growth from first
observation, no more than 35% drawdown, improving recent structure, external recent
transaction activity, at least $10k quoted AMM liquidity and a current ownership
screen. Entries are limited to $10k–$250k valuation and supported PumpSwap quotes.
Insufficient information leaves the coin on the watchlist. These unvalidated
thresholds are research assumptions, not a measured probability of a large return.

The intended horizon is 1–10 days. A 30% modeled loss, ownership flag or missing
quote can trigger an earlier exit; after one day a 35% trailing exit applies, and
10 days is the maximum intended hold. All fills require a later fetched quote.
Missing quotes remain open with a conservative $0 mark and never become invented
sales. The model uses indexed base reserves and price, 2.5% fees plus 1% adverse
fill each side, $0.10 fixed costs and 5% modeled failed attempts. These are explicit
stress assumptions, not on-chain executable quotes or currently measured fees.
DEX quote source age is not supplied by the provider; fetch times are shown instead.

Neither repeat sightings nor a $20k-to-$50k move establishes that a coin will reach
$100m. Persistent observation, ownership evidence and a prospective longer-hold
ledger let us test that hypothesis while keeping unsuccessful coins in memory.

## Portfolio brief and optional AI

`dashboard.config.json` configures the summary service. Its current provider is
OpenAI `gpt-4.1-mini`, using the Responses API and `OPENAI_API_KEY` from the process
environment. The background service loads its key from the private, ignored
`data/service.env`; no key is sent to the browser. A connected
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
wash trading, linked wallets, holder concentration, or creator selling. The separate ownership gate now checks mint controls, supported Token-2022 extensions
and bounded holder/transfer evidence via RPC; the snapshot feed itself still cannot
establish holder identity or organic demand.

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
verification, exhaustive wallet-link analysis, guaranteed feed completeness, or subsecond
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
`experiments/ui-before-20260911/web/`. The redesign preserved the ledger. The later user-authorized cadence update disabled
portfolio loss pauses; the ownership gate then added a prospective entry prerequisite
without resetting the existing portfolio.

## Sources

- [DEX Screener API](https://docs.dexscreener.com/api/reference): public token/pool valuations, liquidity and activity used for continued observation.
- [Solana getProgramAccounts](https://solana.com/docs/rpc/http/getprogramaccounts): full filtered token-account scans.
- [Solana transaction JSON](https://solana.com/docs/rpc/json-structures): parsed token instructions and balance-owner evidence.
- [Solana token extensions](https://solana.com/docs/tokens/extensions): controls assessed by the ownership gate.

- [Pump fees](https://pump.fun/docs/fees): bonding curve and graduated-pool fee schedules.
- [Pump program documentation](https://github.com/pump-fun/pump-public-docs/blob/main/docs/PUMP_PROGRAM_README.md): virtual and real reserves and curve completion.
- [PumpPortal fees](https://pumpportal.fun/fees/): optional route-cost assumption; this app does not call its transaction APIs.
- [PumpPortal data documentation](https://pumpportal.fun/data-api/real-time/): metered trade feeds would be a later data upgrade, not part of this free collector.
- [OpenAI GPT-4.1 mini](https://developers.openai.com/api/docs/models/gpt-4.1-mini): model and reference rates used by the optional summary service.
- [OpenAI Structured Outputs](https://developers.openai.com/api/docs/guides/structured-outputs): schema-constrained fact selection through the Responses API.

`DESIGN.md` describes the broader research architecture. This README describes the
implemented paper baseline and its actual limits.
