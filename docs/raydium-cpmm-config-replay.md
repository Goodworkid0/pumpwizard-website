# Raydium CPMM config replay

This read-only prerequisite validates saved `AmmConfig` account captures before a
shadow quote experiment. It verifies the Anchor account discriminator and exact
236-byte layout, the CPMM account owner, the archived pool/config association,
slot/version/commitment, and the documented 10⁻⁶ fee-rate bounds. It writes a new
proof report only; it does not fetch RPC data, alter the archive, calculate a
quote, or affect the tracker and simulator.

```bash
python3 scripts/replay_raydium_cpmm_config.py \
  --input /absolute/saved-cpmm-config.json \
  --archive /absolute/raydium-market.sqlite \
  --report /absolute/new-cpmm-config-report.json
```

The next quote adapter must bind a config snapshot to a compatible full pool-state
capture and reject Token-2022 transfer-fee mints unless their epoch-specific fee
accounts are also proven. Catalog fee fields remain ineligible for quote math.
