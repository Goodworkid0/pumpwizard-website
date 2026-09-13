# Refreshed V2 forward trial

This is a simulation experiment, not a claim of improved returns. No wallets,
signatures, deposits or real orders are created. The original 139 cohorts keep
their balances, entry/exit rules and full histories. No losing ledger is reset.

## First rollout

`trial_experiments.json` preselects one parent for each of the 14 signal families:
four legacy entry styles and ten research families. Research parents all use the
same $100k–$250k band, 5-minute observation and 7-day maximum hold. This bounded
pilot is not a test of every cap band or all 139 original configurations, and was
not selected by winning historical P&L. There are 56 additional funded instances,
$500 each ($28,000 additional virtual capital, $97,500 total across 195 bots).

Each parent has four new, independent accounts beginning on the same input cycle:

| Arm | Entry policy | Exit policy |
| --- | --- | --- |
| V2 control | Original parent's rule | Original parent's rule |
| V2 ownership telemetry | Original parent's rule; ownership data is recorded only | Original price/time exits |
| V2 exits | Original parent's rule | Original exits plus earlier net-value profit trail |
| V2 combined | Original parent's rule; ownership data is recorded only | Original exits plus profit trail |

Do not compare the old parent's all-time P&L against a fresh version as if both
started together. Use the new V1 control. All four accounts receive the same
live observations, not a replay of historically profitable entries. Research
versions share the same already-observed feature history, while each legacy
arm warms its own identical fresh observation history. These are independent
decision policies, so positions and eventually trade samples will diverge.

Fees, adverse fills, failure probability, position sizing, cap bands, signal
thresholds, absolute targets, holding limits and original stops are unchanged.
The telemetry arms record available ownership evidence alongside their decisions,
but unknown, expired, future, wrong-mint, wrong-policy, or blocked evidence never
blocks a simulated entry and never forces an exit. This keeps the experiment
focused on market-data simulation rather than RPC coverage.

The profit trail arms when previous partial proceeds plus current modeled **net**
liquidation value reach 120% of the initial position cost. It requests liquidation
after that total value falls 10% below its observed peak. It works before the
original first market-cap target, and stays coherent after partial sales. This
first experiment uses a full remaining-position trail, not an additional early
partial take-profit. Signals still require a later usable quote and the normal
fill delay. Stops and ownership/time/quote-gap rules take priority over a new
trail request. An already-pending full liquidation remains pending; its original
reason is retained. Failed attempts cost fees and retry. Gaps can produce losses
even after the trail arms; there is no guaranteed break-even or target fill.

## Inspect and compare

Engine's **Versioned comparison pilots** filter lists the new versions. Open a version
or one of the selected original parents for its four-arm comparison. It shows
net and realized P&L, entries, fully closed trades, closed win rate, maximum
drawdown, closed-trade profit factor and ownership-observation counts. The latter
are repeated observations, not unique coins. Zero closed trades means no
closed-trade evidence. Profit factor uses complete position outcomes including
their sale costs; failed-attempt costs are included separately in realized net
P&L. No-loss records are shown explicitly, not converted to an infinite score.
P&L differences are displayed only when start and latest observation timestamps
match. Open marks are not realized profits; missing usable quotes still mark at
zero under the existing model. No automatic winner promotion is implemented.

Assess net expectancy and drawdown alongside win rate, fees, turnover, quote
coverage and realized/unrealized split. Require adequate closed samples and
different market conditions; inspect outcomes by coin and day because bots
trading the same token are correlated. Do not cherry-pick a small lucky winner.
Subsequent signal-specific changes should get another trial ID and fresh matched
controls. Preserve bankrupt/inactive accounts rather than refunding them.

## Persistence and reproducibility

Every instance has `data/long-hold-t1-<family>-<arm>.sqlite`. Its contract stores
the parent-config hash, full effective configuration, trial arm and thresholds,
ownership configuration and implementation hash. The contract refuses config,
policy or code changes on restart; create a **new trial ID and new ledger** to
change a running version. Original cohort migration behavior is unchanged.

Legacy trial inputs are stored in the instance database. Research trial
`input_refs` point to the exact raw quotes, ownership evidence and computed
features in `data/shared-research.sqlite`. Keep that archive with the ledgers
and the matching Python source bundle for replay. Decisions, profit-trail
arming, pending exits, fills, costs, closed positions, rejected-check counters
and equity points are committed with each instance's state. Normal restart
resumes these records; it does not inject new capital or historical fills.

Verification:

```bash
python3 -m unittest discover -s tests -v
python3 scripts/verify_versioned_trials.py
python3 tests/check_versioned_trials_ui.py
```

Unit and capacity tests use disposable synthetic databases, never live ledgers.
The browser check is read-only against the local running service. A synchronized
comparison is not a guarantee of statistically significant improvement.
