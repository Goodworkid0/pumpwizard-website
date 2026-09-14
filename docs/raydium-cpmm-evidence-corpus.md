# Raydium CPMM evidence corpus

This is the handoff boundary between an operator's saved mainnet evidence and
the separate historical-validation gate. It is deliberately offline: the sealing
command accepts JSON captured elsewhere, creates a temporary archive only, and
writes one new proof report. It has no RPC URL, credential, collector service,
simulation import, public endpoint, or UI effect.

An evidence corpus contains one bounded catalog page, one to 100 saved CPMM
event recordings, one to 100 saved full vault/pool-state recordings, and one
validation recording. All case events, before/after states, and compatible config
captures must be `finalized`. The sealer reconstructs an empty append-only archive
from those raw recordings, replays every reconciliation proof, runs historical
quote validation, and preserves the resulting manifest and input hashes.

```bash
python3 scripts/seal_raydium_cpmm_evidence.py \
  --input /absolute/saved-cpmm-evidence-corpus.json \
  --report /absolute/new-cpmm-evidence-corpus-report.json
```

The report can be ready for human review only when every quote matches its
reconciled historical vault delta. It is **never** a simulation enablement: the
report sets `simulation_eligible` to `false` unconditionally. A future review
must also establish sufficient coverage across swap directions, fee modes, pools,
and known exclusions before a separate Raydium simulation read model is proposed.

The account and transaction payload shapes are based on saved Solana RPC or
observer output. Standard RPC account reads can return up to 100 accounts in a
request, while transaction results include slot and metadata; the corpus still
requires an observer-provided write version and does not invent one. See
[getMultipleAccounts](https://solana.com/docs/rpc/http/getmultipleaccounts) and
[getTransaction](https://solana.com/docs/rpc/http/gettransaction).
