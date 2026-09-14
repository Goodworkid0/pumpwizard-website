# Raydium CPMM event replay

Phase 3b parses **saved** Raydium CPMM transaction captures into immutable
`market_events`. It has no RPC client and is not imported by production tracking,
simulation, the public worker, or the web application.

The recording format is `pumpwizard.raydium-cpmm-event-replay/v1`. Each successful
transaction provides a signature, slot, commitment, observed/received timestamps,
and one to 100 normalized CPMM instructions. A supported instruction has the
documented CPMM program ID, all 13 ordered accounts, and base64 instruction bytes.
The parser accepts only IDL 0.2.0 `swap_base_input` and `swap_base_output`, checks
the pool address/config/mints against the archive, and preserves source hashes in
the event payload and replay report.

```bash
python3 scripts/replay_raydium_cpmm_events.py \
  --input /absolute/saved-cpmm-events.json \
  --archive /absolute/raydium-market.sqlite \
  --report /absolute/new-cpmm-event-replay-report.json
```

The emitted values are swap instruction intent: `amount_in` and
`minimum_amount_out`, or `max_amount_in` and requested `amount_out`. They are not
actual transfers, reserves, fills, prices, or execution quotes. A later comparison
step must reconcile event direction and transfer evidence with vault states at
compatible slots before a quote model can be introduced.
