# Market archive V1 — inactive, verifiable foundation

This patch adds `paper.market_archive.MarketArchive`: an append-only SQLite
archive for Solana token identities, pool identities, decoded market events and
pool-state observations. It is a storage and verification boundary only. It has
no network client, collector wiring, strategy import, dashboard endpoint,
configuration key, service command, dependency or deployment change.

## Production safety boundary

The live command remains `python3 paper_trader.py live`. It does not import or
instantiate `MarketArchive`, so it cannot create its database or alter an existing
ledger. The public process reads its existing registry and SQLite read models;
there is no new route, card, feed or client asset.

| Surface | Before patch | After patch |
| --- | --- | --- |
| Home, dashboard, strategies, research and public API | Existing content and behavior | Identical content and behavior |
| Collector requests and provider cost | Pump/index/RPC behavior already configured | No new requests or provider use |
| Simulated trades, positions, cash and reports | Existing ledgers only | Identical; archive has no execution integration |
| Containers, systemd and deployment configuration | Existing commands and images | No changes |
| Local development | Existing package | Optional new offline archive module and tests |

The file is created only when an operator or a future, separately reviewed
collector explicitly constructs `MarketArchive(path)`. The `view_only=True` mode
requires an existing file and cannot create one.

## Immutable record contract

All records are canonical JSON with SHA-256 content proofs. Re-inserting an
identical row is idempotent; attempting to reuse the identity with different
content raises `ArchiveError`. A pool requires both mint identities first; an
event or state requires its pool first. The archive persists these tables:

| Table | Identity | Purpose |
| --- | --- | --- |
| `tokens` | `solana:<mint>` | Mint, decimals and optional display metadata |
| `pools` | `solana:<program-id>:<pool-address>` | A protocol-neutral pool identity and immutable configuration |
| `market_events` | `solana:<signature>:<instruction-index>` | A decoded pool event with slot, commitment, parser version and original payload |
| `pool_states` | pool ID + slot + write version | Exact parsed pool state needed for later quote models |

`MarketArchive.verify()` runs SQLite integrity checking, re-normalizes every
record, validates every content hash and verifies event/state pool references. It
returns a deterministic manifest with row counts and table proofs. The proof
establishes that the local archive has not changed relative to that manifest; it
does not establish that an upstream provider or parser was truthful.

`scripts/verify_market_archive.py` exposes that same check as a strictly
read-only operator command. It refuses a missing or non-regular path and does not
create an archive:

```bash
python3 scripts/verify_market_archive.py --archive /absolute/staged-market.sqlite
```

## Rollout gate

Run the isolated verification suite before any future collector integration:

```bash
python3 -m unittest discover -s tests -p 'test_market_archive.py' -v
```

Production rollout of this patch requires only the normal release checks because
the deployed runtime behavior does not change. A later phase that writes live
events must add a separate service/config switch, parser fixtures from recorded
transactions, gap/recovery handling, and a shadow-only period before it can affect
research or simulation inputs.
