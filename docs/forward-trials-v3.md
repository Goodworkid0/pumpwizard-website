# Matched V3 forward trial

V3 is an additive, forward-only comparison. It leaves the original cohorts and
the refreshed V2 ledgers untouched, then gives each selected parent four newly funded,
isolated $500 instances on the same live observation cycle.

| Arm | Change from the original parent | Question |
| --- | --- | --- |
| V3 control | None | What does the untouched rule do from this fresh start? |
| V3 confirmation | A valid signal must remain valid for a second fresh observation 60 seconds later | Does an extra confirmation reduce false entries? |
| V3 protection | After modeled net liquidation reaches +10%, request an exit if it later returns to cost | Can modest gains be protected before a full reversal? |
| V3 combined | Both changes | Do the two changes interact beneficially? |

The protection arm is not a guaranteed break-even fill: the request waits for
the normal later usable quote and may fill below cost after a gap or delay. All
other parent settings remain unchanged: market-cap band, signal family, fees,
adverse execution model, sizing, stop, targets, holding limit and source feed.

`v3_trial_experiments.json` begins with four representative parents—breakout,
persistent, multi-window and liquidity—for 16 new instances. The bounded rollout
keeps the service within its current file-descriptor capacity; it is not a
result-based selection. Each contract stores its full V3 definition and
implementation hash. Any later policy adjustment needs a new trial ID and
separate ledgers.

Evaluate closed trades, unique coins, drawdown, fees and coverage together. A
zero-trade or low-trade arm is not a winner; it is insufficient evidence.
