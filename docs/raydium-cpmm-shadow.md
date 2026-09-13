# Raydium CPMM shadow catalog

`paper.raydium_cpmm` is phase 2 of the multi-market work. It discovers only
Raydium CPMM **identities** from one bounded public catalog page. It is not
imported by `paper_trader.py`, `BrainService`, the public worker, or the web
application. Running it is an explicit operator action and cannot create a
trade, quote, signal, strategy instance, dashboard item, or provider request from
the existing service.

Raydium’s “Standard” catalog type includes both legacy AMMs and CPMM pools. The
adapter accepts a record only when it has the CPMM `config` object and no legacy
`marketId`. It stores token identities and the program/pool/config identity in
`MarketArchive`. The raw provider page and its hash are stored in a new immutable
report. Reported TVL, price and token amounts are checked for finite values but
remain report-only: they are not a slot-confirmed pool state and never price a
simulated order.

## Offline rehearsal

Provide a saved Raydium catalog page first. Both archive and report paths are
operator-selected; the report must be new.

```bash
python3 scripts/shadow_raydium_cpmm.py \
  --input /absolute/saved-raydium-page.json \
  --archive /absolute/raydium-market.sqlite \
  --report /absolute/new-raydium-shadow-report.json
python3 scripts/verify_market_archive.py --archive /absolute/raydium-market.sqlite
```

For a one-time catalog fetch, use `--fetch --size 100` instead of `--input`.
The command permits only the fixed HTTPS Raydium catalog endpoint
`/pools/info/list-v2`, rejects redirects and response bodies over 4 MiB, and does
not use transaction, wallet, signing, or swap endpoints.

## Deliberate boundary

This establishes a bounded, inspectable candidate universe. It does not yet
establish the state needed for an executable CPMM quote. The next implementation
must collect account writes/swaps with slot, write version, commitment, vault
balances and parser fixtures, then archive them as `pool_states` and
`market_events`. Only after that evidence is shadow-replayed can a CPMM quote
adapter be considered for a separate paper strategy.
