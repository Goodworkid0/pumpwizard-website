# Observed market log

`ObservedMarketLog` is the append-only store for the lower-assurance Raydium
CPMM standard-RPC reports. It is intentionally **not** a `MarketArchive`: public
standard RPC gives a context slot but no account write version, so these records
cannot be claimed as strict replay or transaction reconciliation evidence.

Each pool has a local sequence beginning at 1. A report can be recorded again
unchanged (idempotent), but a conflicting record or a sequence gap is rejected.
This makes missed collection intervals visible rather than silently smoothing
them over. The source report keeps its RPC request/response data and hashes; the
log adds an immutable content hash and deterministic manifest.

```bash
# First capture a new report with a chosen next sequence, then record it.
python3 scripts/record_raydium_cpmm_observed.py \
  --store /absolute/raydium-observed.sqlite \
  --report /absolute/new-observed-cpmm-snapshot.json

# This is offline and read-only.
python3 scripts/verify_observed_market_log.py \
  --store /absolute/raydium-observed.sqlite
```

The log creates no market archive rows, orders, paper fills, strategies, or
public API/UI changes. Its only supported assurance is **observed simulation**;
freshness, collection continuity, commitment, and source limitations must remain
visible to any later simulation or public presentation.

For a bounded capture command that allocates its sequence directly from this
log, see [the observed monitor](raydium-cpmm-observed-monitor.md).
