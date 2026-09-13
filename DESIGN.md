# Pump trading research agent — initial design

Status update, 10 September 2026: the off-chain paper baseline is implemented.
See README.md for its actual behavior and limitations and paper.config.json for
runtime settings. The broader architecture below remains a research design.
No real trader, wallet, or paid subscription is connected, and no profitability
is claimed. Real experimental capital remains unset; virtual capital defaults
to $1,000 with $20 fictional positions.

Latest scope: short-term intraday trading. The proposed token entry range is
$10,000–$50,000 in quoted market capitalization, pending confirmation that the
user's "10–50k range" refers to token valuation rather than trading capital.
The strategy-specific paper configuration is in strategy.paper.json. No capital
allocation is inferred from that valuation range.

The objective is to determine whether a narrowly defined strategy can earn positive
returns after execution and operating costs. The user would operate any eventual
live execution service through a separately funded wallet. The assistant can help
develop and test the software, but does not take custody or place live trades.

## What the earlier wallet audit teaches us

The analyzed wallet had 4,604 economic trades and approximately $326,601 in trading
cash surplus after costs within trades and failed fees. This is not its verified
economic profit: some inventory arrived from other wallets with unknown basis.
Among 1,194 fully closed tokens with traceable purchases, the net result was $13,075,
the win rate was 23.2%, and removing the largest winner changed the result to a
$6,486 loss. Those closed-token numbers are before allocating failed-attempt fees.

Therefore, following this wallet is an experimental signal, not a demonstrated
profitable copy-trading strategy. We must account for follower delay, price impact,
different position sizes, transfer-derived inventory and missed exits. Its historical
trade ledger alone does not provide the full market history needed for a causal
backtest: it contains selected trades, not every candidate available at each moment.

Source: ../pump-wallet-analysis-q1x4/report.md

## Service design

1. Collector: capture new launches, migrations, pool state, trades, observed wallet
   activity, holder distributions and quote responses. Preserve both event time and
   arrival time, sequence numbers, source, and raw records. Reconnect with backfill
   and deduplication. A gap makes signals ineligible until resolved.
2. Research model: summarize candidate evidence and classify risks or narratives.
   It returns structured observations with sources and timestamps. Coin names,
   websites and social posts are untrusted data, never operational instructions.
   The model cannot access keys, change limits, or directly instruct a signer.
3. Strategy engine: deterministic, versioned rules turn recorded features into
   entry/exit proposals. Every decision, including abstentions, is recorded. Changes
   create a new experiment version; the agent cannot change its own live policy.
4. Budget and execution checks: verify current balances, pending reservations,
   per-token exposure, aggregate exposure, fees, quote age, price impact, allowed
   route and loss-halt status. This component independently permits or rejects an
   order. Commit reservations atomically before submitting any eventual order.
5. Paper executor: replay observed events after measured decision/transport delay.
   Use exact-size quotes or validated pool arithmetic; include swap fees, routing
   fees, priority fees, tips, price impact, failures and rent. No fills at a chart's
   last price; no sale booked when an exit cannot actually be quoted/executed.
6. Ledger/dashboard: reconcile fills, confirmed transactions and token accounts;
   show cash, realizable inventory, realized/unrealized P&L, costs, drawdown, and
   reason codes. Transfers, deposits and rebates are distinct from trading returns.
7. Optional later live adapter, operated by the user: validates unsigned transaction
   instructions and balance limits, signs locally with a separate wallet, submits
   once, reconciles the signature, and only retries after determining its state.
   It never requests an automatic top-up or accesses the user's main wallet.

The first implementation should cover one strategy and one execution route in
paper mode. Multi-agent debates, self-modifying strategies and broad social scraping
are unnecessary before the baseline can survive realistic costs.

## First strategy hypothesis to test

Test confirmed momentum in Pump.fun-origin tokens whose quoted valuation is
$10,000–$50,000 when the entry could actually execute. Market capitalization is
an entry filter, not a target profit, a funding amount, or a measure of available
exit liquidity. Record whether the provider's figure uses total or circulating
supply; do not silently compare incompatible valuation definitions. Keep the
quote-asset USD conversion fresh. Determine the actual pool and route instead of
inferring bonding-curve status from the market cap alone.

Watch before entry: collect at least 60 seconds of usable observations and compare
15-second and 60-second flows. Look for increasing distinct buyers and sustained
net buying, enough exit depth for the proposed position, and participation not
dominated by a few linked accounts. These are features to evaluate, not established
predictors of profitable returns. Trading volume alone can reflect wash activity.
Investigate creator sales, holder concentration, mint/freeze authorities,
Token-2022 extensions, and whether the intended sell can be built and simulated.

Split results into $10k–$20k, $20k–$35k and $35k–$50k entry bands. Do not conclude
that the lowest valuation band is the cheapest or best: the relevant test is
net expectancy at achievable position size and execution delay. Use [lower, upper)
intervals for the first two bands and [35k, 50k] for the last to avoid double counting.

The first paper baseline exits the entire position on +20% net executable return,
−10% net executable return, or a 10-minute time limit, whichever triggers first.
These are provisional experiment parameters, not optimized rules or promises.
Net executable return compares estimated proceeds from selling the entire position
after all exit costs with its actual modeled acquisition cost including entry
costs. If an exit cannot execute, record an open, impaired position and a pending
exit rather than inventing a fill. The loss trigger does not cap the actual loss.

Evaluate quotes for the exact planned size, with the simulated entry's own reserve
impact reflected in the modeled exit. Position size must satisfy both the capital
policy and a depth/price-impact limit. No funded orders are eligible until those
limits and the experimental budget are specified. Skip a candidate if costs leave
insufficient economic room; never increase order size just to make fixed fees look
smaller. No averaging down is included in this first baseline.

The market-cap entry range never blocks an existing position's exit. A position
can move above $50k or below $10k while being managed. Crossing the band is not by
itself evidence that the trade should be held, sold, or enlarged. After a data gap,
stop new entries and rebuild observation windows; preserve risk-reducing exit logic.

Keep the model outside the timing-critical execution path. Research can refresh
candidate context in the background; observation windows, quote checks and exits
run deterministically with measured latency. Test slower execution scenarios before
claiming the strategy can capture a move observed on the chart.

Compare that hypothesis against a simple fixed momentum rule, delayed wallet
following, and a no-trade cash baseline over the same contemporaneous universe.
Signals and parameter ranges must be fixed before the evaluation period. These
filters reduce some risks; they do not establish token safety or a profitable edge.

Avoid assuming that being the first buyer or copying a profitable wallet is the
edge. A research model is useful for contextual work, while timing-sensitive rules
and order handling should run in ordinary code with measured latency.

## Proposed budget policy

The adjacent policy.example.json is an unapproved proposal, not an active policy.
Its null capital and service budgets block any funded integration. Initial ratios
are experiment defaults to discuss, not optimized settings or investment advice.

- Start with simulated capital. Specify trading capital separately from data, RPC,
  model and hosting costs; total profitability must deduct both.
- Maximum proposed entry spend: 2% of the initial experimental trading allocation,
  including estimated fees; at most three concurrent positions and 6% aggregate
  acquisition exposure, counting pending orders. No automatic compounding.
- Maintain a separate fee/exit reserve. Skip trades whose costs make the proposed
  size uneconomic instead of silently increasing position size.
- A 2% daily equity-loss trigger and 10% experiment drawdown trigger stop new
  entries and cancel unsubmitted entry proposals. Conservative exit-value estimates,
  accumulated fees and pending exposures count toward these triggers. They are
  halt thresholds, not guaranteed maximum losses: markets can gap or become unsellable.
- Entry halts must preserve the ability to submit validated risk-reducing exits.
  Restarting cannot reset cumulative loss, cost or open-order state.
- No leverage, automatic funding, transfers to arbitrary destinations, or size
  escalation after losses. No unbounded retries and no repeated replacement of an
  order whose existing signature has an unknown outcome.

Before live use, define permitted strategies and routes, initial capital, fee reserve,
per-order slippage and price-impact limits, daily operating-cost allowance, drawdown
triggers, exit policy, and user-controlled stop/resume behavior.

## Evidence required before considering a funded pilot

Collect prospective data across launches, including dead and illiquid tokens.
Use chronological evaluation with a frozen strategy, realistic delays and adverse
execution assumptions. A few profitable days or a fixed trade count is not proof.

Report net expectancy per independent position episode, turnover, win/loss sizes,
profit factor, marked portfolio drawdown, fill rate, failed-attempt costs, and both
SOL and USD results. Use uncertainty estimates grouped by token and day so repeated
partial fills are not treated as independent evidence.

Show profit concentration and results with the largest one, three and five winners
removed. A strategy built to capture rare winners need not remain profitable after
every such removal, but its dependence on them must be understood and replicated
across independent evaluation periods. Compare modeled fills with observed quotes
before drawing conclusions from paper profits. A small user-operated live pilot
can later measure execution mismatch; scaling is a separate decision.

## Current API choices and costs, verified 10 September 2026

Pump.fun lists a 1.25% total fee per bonding-curve trade; canonical PumpSwap fees
vary by market capitalization and quote currency. Fetch actual fee configuration
and route quotes rather than hard-coding every trade to 1.25%.
https://pump.fun/docs/fees

PumpPortal, a third-party service, lists a 0.5% Local Transaction API fee on each
trade, additional to network and Pump protocol fees. The simplest bonding-curve
round trip through this route therefore has approximately 3.5% combined percentage
fees before network fees, price impact and slippage. Its local API returns an
unsigned transaction for local signing; it does not need custody of the trading key.
https://pumpportal.fun/fees/
https://pumpportal.fun/local-trading-api/trading-api/

PumpPortal's new-token and migration messages are free. Token/account trade streams
are metered at 0.01 SOL per 10,000 events and require an API key with a linked funded
wallet. Use one shared websocket and an explicit subscription/message-cost budget.
No metered stream has been connected for this design.
https://pumpportal.fun/data-api/real-time/

Jupiter's current Swap V2 order/execute flow is a possible alternative when a token
has a supported route. Older Ultra V1 examples are deprecated. Route availability
must be tested; do not assume new bonding-curve launches can be routed through it.
https://developers.jup.ag/docs/swap/order-and-execute

These documented capabilities establish technical feasibility, not profitability.
