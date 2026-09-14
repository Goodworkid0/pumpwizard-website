# Raydium CPMM observed-paper pilot

This pilot is an explicit, finite, observed-only strategy lane. It does not use
a wallet, submit an order, or share a balance, position, or P&L record with
Pump.fun.

The deployed NEAR/USDC pilot configuration is
`deploy/raydium-near-usdc-pilot.json`:

- a separate 500-USDC virtual balance (raw USDC units);
- maximum 50-USDC paper entry and one open position;
- three consecutive immutable, finalized standard-RPC snapshots;
- a 0.5% probe-quote increase before entry; and
- a ten-snapshot maximum holding window.

Each decision quotes a fixed one-NEAR probe against exact archived snapshots.
The decision output retains the sequence, snapshot hash, and quote hash. A
threshold miss writes no paper event. A qualifying entry and later exit are
written only to the separate observed ledger, which is content-addressed and
replay-verifiable.

Run one decision manually:

```bash
python3 scripts/run_raydium_cpmm_pilot.py \
  --archive data/raydium-identities.sqlite \
  --store data/raydium-observed.sqlite \
  --ledger data/raydium-near-usdc-pilot.sqlite \
  --config deploy/raydium-near-usdc-pilot.json
```

Use `--inspect` to evaluate evidence without creating a ledger file or event.
The bounded monitor runs a finite decision schedule against an already-running
snapshot monitor:

```bash
python3 scripts/monitor_raydium_cpmm_pilot.py \
  --archive data/raydium-identities.sqlite \
  --store data/raydium-observed.sqlite \
  --ledger data/raydium-near-usdc-pilot.sqlite \
  --config deploy/raydium-near-usdc-pilot.json \
  --interval-seconds 60 --max-decisions 60
```

Standard RPC cannot prove account write versions. This lane is therefore an
observed simulation only: it is not an executable quote, transaction replay, or
claim of on-chain execution. A separate market-scoped public projection and
download export are required before this pilot appears in public Live Data.
