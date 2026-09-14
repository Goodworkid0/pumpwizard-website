# Raydium CPMM shadow quote

This is a read-only historical quote experiment. It accepts one
`pumpwizard.raydium-cpmm-shadow-quote/v1` request and writes a new proof report;
it does not fetch RPC data, submit a transaction, alter the market archive,
create a signal, or enter the current simulator and website.

The request must identify an archived full pool state, include a self-consistent
reconciliation proof anchoring that state to a parsed swap event, and contain one
same-slot CPMM config capture. The adapter verifies those hashes against the
current append-only archive, uses fee-free reserves and the documented constant
product/fee rounding rules, and only supports classic SPL Token vaults. It rejects
Token-2022 mints because their epoch-specific transfer fees are not yet captured.

```bash
python3 scripts/quote_raydium_cpmm.py \
  --input /absolute/cpmm-shadow-quote-request.json \
  --archive /absolute/raydium-market.sqlite \
  --report /absolute/new-cpmm-shadow-quote-report.json
```

The result is a historical calculation, not an executable quote or simulated
fill. Before any simulator integration, compare a substantial saved sample of
these results with parsed swap events and vault changes, including creator-fee
modes and any token-program edge cases.
