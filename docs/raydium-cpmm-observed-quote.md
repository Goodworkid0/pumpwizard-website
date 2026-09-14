# Raydium CPMM observed quote

This read-only adapter calculates a base-input constant-product estimate from a
specific immutable record in `ObservedMarketLog`. It validates the full pool,
vault, and AmmConfig account bytes again against the read-only catalog identity,
uses the CPMM fee math, and supports classic SPL Token pools only.

```bash
python3 scripts/quote_raydium_cpmm_observed.py \
  --archive /absolute/raydium-identities.sqlite \
  --store /absolute/raydium-observed.sqlite \
  --pool-id 'solana:PROGRAM:POOL' --sequence 42 \
  --input-mint MINT --amount-in 1000000
```

The result is an **observed simulation** estimate linked to its source snapshot,
context slot, commitment, and hashes. Standard RPC does not provide account
write versions; this adapter neither creates one nor claims reconciliation,
execution, a fill, or a live price. It is the input boundary for the future
separate Raydium paper ledger, not a trading route.
