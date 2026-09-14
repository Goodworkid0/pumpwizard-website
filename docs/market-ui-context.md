# Market-scoped public UI

The public site accepts a `market` query parameter on its market-facing pages.
The shared navigation selector is the source of that context and preserves it on
Live View, Engine, Research, Legal, Contact, and affiliate links.

| Context | Public behavior | Data boundary |
| --- | --- | --- |
| `market=pumpfun` | Existing live simulation dashboard | Existing Pump.fun/PumpSwap public read models only |
| `market=raydium-cpmm` | Same dashboard/engine/research shell, with observed-simulation labels and unavailable states | No Pump.fun value may be fetched or displayed; Raydium P/L/positions remain unavailable until its read model is published |

`pumpfun` is the default for legacy links without a market parameter. Invalid
values also fall back to that default rather than selecting an unknown market.

Raydium uses the same familiar page structure as Pump.fun—navigation, portfolio,
positions, engine, and research—but its page-specific Pump.fun read clients stay
disabled. Empty fields therefore represent unavailable Raydium data, not zero
P/L or a hidden Pump.fun value. The server remains read-only and provides no
market-selection write path.

When Raydium has a representative passing validation corpus, the next change is
a dedicated market-scoped public read model. It must expose a separately keyed
portfolio, positions, strategies, and provenance/coverage metadata. Aggregated
"all markets" views come only after those ledgers are independently available;
they must never merge balances or P/L by accident.
