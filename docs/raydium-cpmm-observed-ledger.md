# Raydium CPMM observed paper ledger

`ObservedRaydiumLedger` is a separate paper ledger for explicit Raydium CPMM
entries and exits. It consumes only the immutable observed quote adapter and
opens both the catalog archive and observed log read-only. It cannot share a
database, virtual balance, position, or P&L record with Pump.fun.

The first version is deliberately quote-token-denominated. A quote-token input
opens a simulated base-token position; a later recorded snapshot estimates the
base-token exit back into the quote token. It reports raw quote-token P&L and
refuses to convert that to USD without a separately captured FX observation.

Entries, exits, and marked positions reject snapshots older than the configured
maximum age. Failed/stale marks remain impaired rather than being valued at zero.
Every event is content-addressed and replayed by `verify()`.

This is **observed simulation only**: a model based on standard-RPC snapshots,
not an order, a wallet, an executable price, or a reconciliation claim. The next
layer will provide strategy policy and an explicitly read-only public projection
for the existing market-scoped dashboard.

The command below performs one explicit paper action. The same fixed contract
arguments must be supplied on every later invocation; changing them requires a
new ledger file.

```bash
python3 scripts/run_raydium_cpmm_observed_ledger.py \
  --archive /absolute/raydium-identities.sqlite \
  --store /absolute/raydium-observed.sqlite \
  --ledger /absolute/raydium-observed-ledger.sqlite \
  --pool-id 'solana:PROGRAM:POOL' --initial-quote-amount 1000000000 \
  open --position-id pilot-1 --sequence 42 --quote-amount 10000000
```
