# Raydium CPMM observed capture

This is the free-RPC, Pump.fun-equivalent data lane. It captures one current
Raydium CPMM pool account, its two vaults, and its AmmConfig using only bounded
standard Solana RPC reads. It writes a new immutable JSON snapshot report and
never writes the market archive, starts a strategy, creates a paper fill, or
changes the public UI.

```bash
python3 scripts/capture_raydium_cpmm_observed.py \
  --archive /absolute/raydium-identities.sqlite \
  --pool-id 'solana:PROGRAM:POOL' \
  --sequence 1 \
  --report /absolute/new-observed-cpmm-snapshot.json
```

Without `RAYDIUM_OBSERVED_RPC_URL`, the command uses the official public mainnet
endpoint. It makes exactly two read-only calls: a pool bootstrap read followed by
a four-account `getMultipleAccounts` batch constrained by the bootstrap slot.
The report retains both raw request/response pairs and their hash, RPC context
slots, commitment, capture time, and a local sequence.

Standard RPC does not provide the account write version needed by the verified
lane. The collector records that fact as `unavailable_not_invented`; its output is
eligible only for a future **observed simulation** with visible freshness/gap
telemetry, never transaction-by-transaction reconciliation or executable claims.
