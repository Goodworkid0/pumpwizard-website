# Paper cadence comparison — 11 September 2026

Both presets replayed the same 6,656 saved Pump index observations over 48.53
minutes in isolated databases. The recorded data was read from a consistent,
read-only SQLite transaction. The replay does not create live or on-chain orders.

| Preset | Purchases | Closed | Open | Net modeled P&L | Maximum buys / rolling minute |
|---|---:|---:|---:|---:|---:|
| Previous frequent | 5 | 5 | 0 | −$3.71 | 5 |
| Faster, full bankroll | 73 | 69 | 4 | −$81.43 | 3 |

The previous preset hit its daily-loss pause. The faster preset disables that
pause, uses smaller positions and shorter signal/hold windows, and caps buy
attempts at three per rolling minute. Its lower burst ceiling still produces
more sustained activity. This is a combined cadence-and-loss-policy experiment;
the purchase increase cannot be attributed to one parameter alone.

P&L includes conservative marks for open positions and modeled fees. Both results
are losses. This short in-sample replay does not establish future profitability.
Only tokens watched by the earlier collector are available, which also limits
exit observations for positions the alternate strategy would have held.

Inputs and detailed results: `exports/cadence-comparison-20260911/`.
Current forward experiment: `data/cadence.sqlite`.
Frozen previous code: `experiments/frequent-before-cadence-20260911/`.
