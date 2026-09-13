# Raydium CPMM recorded vault replay

This is phase 3a of multi-market support. It imports a **saved** recording of
Raydium CPMM SPL Token vault accounts into `MarketArchive.pool_states`; it has
no HTTP/RPC client and is not imported by the tracker, simulator, web process,
or public worker.

Each recording carries the schema `pumpwizard.raydium-cpmm-vault-replay/v1` and
one to 100 captures. A capture must supply its archived pool ID, slot, write
version, commitment, observation time, and the two vault account address/owner/
base64-data triples. The replay checks the account owner is SPL Token or
Token-2022, verifies the base 165-byte token-account layout, and confirms its
mint agrees with the archived pool side before it records a balance. The proof
report preserves the full input and links each stored state to its capture hash.

```bash
python3 scripts/replay_raydium_cpmm_vaults.py \
  --input /absolute/saved-cpmm-vault-recording.json \
  --archive /absolute/raydium-market.sqlite \
  --report /absolute/new-cpmm-vault-replay-report.json
python3 scripts/verify_market_archive.py --archive /absolute/raydium-market.sqlite
```

This does not infer swaps, fees, price, or tradeability. The next distinct
boundary is an offline parser for recorded CPMM transaction instructions/logs,
with signatures and instruction indices, followed by a shadow replay comparison
against these slot-versioned vault states. A quote model must wait for that
comparison.
