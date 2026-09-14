# Raydium CPMM shadow validation

This offline gate compares the shadow calculator with a saved, reconciled
historical Raydium CPMM base-input swap. It does not contact RPC endpoints,
write to the archive, create a signal, alter the paper trader, or expose data in
the website.

Each `pumpwizard.raydium-cpmm-shadow-validation/v1` case contains an event ID
and a quote request. The quote must use the full archived pool state immediately
before that exact event. Its reconciliation proof is replayed from its saved raw
recording against the current archive, so reported vault deltas cannot simply be
asserted by a corpus author. The request amount must equal both the instruction's
base-input amount and the observed input-vault delta. The report then compares
the calculated output with the reconciled output-vault delta.

```bash
python3 scripts/validate_raydium_cpmm_quotes.py \
  --input /absolute/cpmm-validation-corpus.json \
  --archive /absolute/raydium-market.sqlite \
  --report /absolute/new-cpmm-validation-report.json
```

The command always creates a new report and opens an existing archive in
read-only mode. A report can contain mismatches for investigation; its `summary`
is the production decision signal. No simulator or UI integration is permitted
until a representative corpus has a documented passing result, including both
swap directions, creator-fee modes, and excluded token-program cases.
