# Raydium CPMM event/state reconciliation

Phase 3c is a strictly read-only gate. It compares archived CPMM swap-instruction
intent with selected before/after vault states and writes a separate proof report;
it does not add records, fetch RPC data, produce prices, or enter the tracker or
simulator runtime.

A `pumpwizard.raydium-cpmm-reconciliation/v1` input supplies one to 100 event
IDs and exact state locators (pool state slot + write version). The gate requires
the states to bracket the event slot, verifies the input reserve rose and output
reserve fell in the instruction direction, and checks those deltas remain within
the instruction's maximum-input/minimum-output bounds. It intentionally does not
call those deltas fills: transfer-fee and account-write evidence still needs a
dedicated collector before execution semantics can be established.

```bash
python3 scripts/reconcile_raydium_cpmm.py \
  --input /absolute/cpmm-reconciliation.json \
  --archive /absolute/raydium-market.sqlite \
  --report /absolute/new-cpmm-reconciliation-report.json
```

The command opens the archive with SQLite read-only mode and refuses an existing
report path. This is the final evidence gate before designing a separate,
shadow-only CPMM quote experiment.
