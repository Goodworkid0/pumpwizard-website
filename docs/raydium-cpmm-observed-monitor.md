# Raydium CPMM observed monitor

This is a finite, opt-in collector for the free-RPC observed lane. It opens the
catalog archive read-only, obtains the next sequence from `ObservedMarketLog`,
captures a standard-RPC snapshot, and appends it before printing its health
record. It never starts automatically, creates no paper fill, and changes no
public page or strategy.

```bash
# One capture (the default): useful for a scheduler or service supervisor.
python3 scripts/monitor_raydium_cpmm_observed.py \
  --archive /absolute/raydium-identities.sqlite \
  --store /absolute/raydium-observed.sqlite \
  --pool-id 'solana:PROGRAM:POOL'

# A bounded local sampling run. It ends after 60 captures.
python3 scripts/monitor_raydium_cpmm_observed.py \
  --archive /absolute/raydium-identities.sqlite \
  --store /absolute/raydium-observed.sqlite \
  --pool-id 'solana:PROGRAM:POOL' \
  --interval-seconds 60 --max-captures 60
```

The monitor requires a minimum 15-second interval and never has an unbounded or
background mode. A failed RPC call stops the command; previously appended
snapshots remain unchanged. Health reports call out the first baseline, each
consecutive local sequence, snapshot age, commitment, the absence of a write
version, and a cadence gap when the next recorded observation is more than 1.5
times the requested interval after the preceding one.

This is **observed simulation only**. It does not establish an executable price,
an on-chain fill, transaction ordering, or transaction-by-transaction replay.
