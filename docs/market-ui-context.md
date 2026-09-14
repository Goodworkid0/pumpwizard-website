# Market-scoped public UI

The public site accepts a `market` query parameter on its market-facing pages.
The shared navigation selector is the source of that context and preserves it on
Live View, Engine, Research, Legal, Contact, and affiliate links.

| Context | Public behavior | Data boundary |
| --- | --- | --- |
| `market=pumpfun` | Existing live simulation dashboard | Existing Pump.fun/PumpSwap public read models only |
| `market=raydium-cpmm` | Evidence-validation status page | No P/L, positions, strategies, signals, or wallet values are published |

`pumpfun` is the default for legacy links without a market parameter. Invalid
values also fall back to that default rather than selecting an unknown market.

Raydium's status page makes the readiness gates visible without implying that a
simulation exists. It disables the page-specific public read clients, so it does
not fetch or display Pump.fun data under a Raydium label. The server remains
read-only and provides no market-selection API or write path.

When Raydium has a representative passing validation corpus, the next change is
a dedicated market-scoped public read model. It must expose a separately keyed
portfolio, positions, strategies, and provenance/coverage metadata. Aggregated
"all markets" views come only after those ledgers are independently available;
they must never merge balances or P/L by accident.
