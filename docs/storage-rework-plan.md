# Storage rework — measured proposal, 2026-09-13

Status: Telegram backup production timer stopped/disabled, backup service inactive,
and `backup_enabled: false` persisted in both production and local private config.
New daily, smoke and upload commands skip before opening credentials or transport.
Existing recovery files and Telegram objects are retained. Site/disk monitoring
continues; backup-specific checks report intentional disablement. This document
plans the database migration; that migration has not been applied.

## Evidence and budget

The production read-only inventory at Unix time 1789336265.6586 took 0.95 seconds.
All sizes below use decimal GB; 1 GiB = 1,073,741,824 bytes.

| Database group | Files | Allocated file bytes | Size |
| --- | ---: | ---: | ---: |
| Instance ledgers embedding market inputs | 71 | 49,745,051,648 | 49.75 GB |
| Instance ledgers using shared references | 180 | 978,743,296 | 0.98 GB |
| Shared archive and other databases | 11 | 1,270,382,592 | 1.27 GB |
| Total SQLite files | 262 | 51,994,177,536 | 51.99 GB |

WAL files and published artifacts are additional. The 251 stored ledgers include
retired instances; the active public catalog has 211 entries.

A sample of the most recent 64 input rows per database contained 4,608 occurrences
but only 193 input IDs. Encoded payload bytes dropped from 141,879,926 to 5,691,519
when counted once per ID: 95.99% duplicate bytes in this sample. One sampled input
ID appeared in 59 ledgers. This compares IDs/encoded sizes, not every historical
payload hash; it is evidence for prioritization, not an exact reclaim guarantee.

The verified recovery had 23.61 GB of SQLite data roughly 20.7 hours before a live
measurement of 52.68 GB including WAL. Observed growth was approximately 31 GiB/day.
At about 103 GiB free, raw exhaustion would be around 3.3 days if that short-window
rate persisted; the operational reserve is reached earlier. Pausing backups avoids
new backup allocations but does not slow the collector's writes.

Planning targets for the existing fleet:

- At least 90% reduction in repeated input payload storage.
- Roughly 3–6 GB after historical conversion and compaction is a hypothesis to
  validate by counting ALL unique payload IDs/bytes, references, indexes and state.
- Target total growth below 2 GiB/day at the current fleet and observation rate;
  reference/equity rows still grow with instance count. Measure actual byte growth
  over 24 hours and report the rate even if the target is missed.
- Establish a hard byte budget for raw input, derived features, instance history,
  indexes, WAL and temporary migration files before activating more instances.

## Why the current design grows

`BrainService` builds one transition and passes it to every runner. Each legacy
`LadderRunner.step` compresses and writes that entire batch into its own `inputs`
table. Versioned Ladder subclasses inherit this behavior. The payload already has
zlib compression; another compression layer will not remove cross-ledger copies.

`ResearchRunner` already records small `input_refs` entries, with feature-bearing
payloads stored once in `shared-research.sqlite`. The raw Ladder batch and the
Research batch containing derived features have different hashes and must remain
distinct even when timestamps match. Deduplication must compare complete payload
identity, never timestamp alone.

Moving the existing schema to PostgreSQL by itself retains these duplicate rows.
Keep SQLite for the first storage fix; PostgreSQL capacity/concurrency migration
can follow with the shared-input representation already defined.

## 1. Stop new duplicate writes

Implement a shared input archive and a small reference per instance transition.
Prefer extending the existing archive's content-addressed `inputs` table, retaining
raw and feature-bearing variants with their exact existing hashes/bytes. Add
explicit format/provenance metadata rather than conflating those variants.

Suggested logical model:

- Shared archive: `inputs(id, at, payload)` plus storage-format metadata.
- Instance ledger: `input_refs(id, at)` plus unchanged state, contract, events,
  sales, trades and equity tables.
- Separate storage migration metadata with source format, cursor, counts, hashes,
  completed-at timestamp and archive identity.

Archive the shared payload durably before committing any instance reference.
Within each instance transaction, commit the reference, state, trade/event writes
and equity together. A crash may leave an unused archive input; it must never leave
a committed reference whose payload is missing. Preserve instance timestamp
idempotency and refuse any ID/content conflict. Rollback/retry must not duplicate
trades, sale legs or equity observations.

Apply this through a storage adapter wired in the existing backend activation
path before collector threads start. Keep immutable strategy implementation files
and contracts byte-identical where feasible: V2/V3 runners reject changed code
hashes. If the adapter cannot preserve that contract, an explicit storage migration
with recorded old/new provenance and replay checkpoints is a prerequisite; do not
override the immutable-contract check or create newly funded strategies.

Support mixed historical embedded inputs and new references during rollout.
Provide one ordered input reader for original/V2/V3 Ladder and Research runners;
deduplicate identical IDs across both sources, reject conflicting bytes, and
preserve every actually consumed observation. Replay/export tools currently choose
input storage from runner class; update them to use storage format instead.

Verification before activation: all six runner types produce identical state and
ordered trade/sale/event/equity proofs on the same inputs; duplicate steps, failed
transactions, crash windows and restart are covered. Validate public analysis,
trade history, copy configuration, research downloads and replay packages against
the mixed storage format. Measure new byte growth on an isolated real-data replay.

## 2. Migrate and compact historical ledgers

Start on the existing local frozen data to calculate the full unique payload set
and actual destination size without making another full production copy.

1. Stream all old input rows, checking that SHA-256 of the original decompressed
   canonical bytes equals the stored ID. Preserve the original encoded bytes;
   decode only for validation. Insert once into the shared archive. Reject
   conflicting hashes, timestamps or payloads and record a resumable cursor.
2. Build each compact instance ledger in a new file using the exact existing
   schema/rows for all non-input tables (including unknown tables), replacing
   embedded input payloads with references. Preserve IDs, ordering, contracts,
   starting balances, timestamps, retired ledgers and current positions.
3. Compare all non-input table counts and ordered row hashes. Resolve every new
   reference and compare its timestamp/hash/payload with the old input. Reconcile
   balances, realized/unrealized P&L, fees and latest equity. Exact replay across
   historical code changes still requires the matching releases/checkpoints.
4. Pre-copy and verify production historical prefixes while collection continues;
   use a short coordinated writer stop for the final tail, WAL checkpoint and
   file switch. Per-file online reads alone are not globally atomic. Public
   processes must reopen switched files together with collector activation so no
   reader continues using an old inode. Verify all 251 ledgers after restart.
5. Keep original files until destination verification and rollback rehearsal pass.
   Then reclaim their space in an explicit audited cleanup with an exact file
   allowlist. Avoid full-database VACUUM across the fleet and avoid temporary copies
   of all embedded inputs. Budget only the unique archive, compact ledgers, final
   tail, one-file work area and recovery reserve; refuse a plan that exceeds free
   space. Preserve WAL/SHM consistency at every switch.

Dropping rows alone does not shrink SQLite files. Compact replacement files are
what return space to the filesystem. Once the new collector advances, restarting
old ledgers would lose newer history: rollback must resolve current references
back into a compatible layout or use a verified current checkpoint, never simply
restart the stale pre-migration directory.

## 3. Bound storage as the lab grows

After lossless deduplication is verified, propose measured retention by data type:

- Keep configs, contracts, trades, sale legs, decision evidence and experiment
  identity for longitudinal comparisons.
- Keep the hot feature-calculation window with sufficient restart overlap.
  Derived historical features can move to sealed chunks only when reconstruction
  from raw input and implementation provenance is demonstrably equivalent.
- Keep raw inputs available for replay. Seal time-partitioned immutable chunks
  with checksums and per-instance range indexes so shared inputs are stored once.
  Cold data still costs space if it stays on the same disk; moving it elsewhere
  needs a concrete destination/cost. Any deletion or shorter retention changes
  research capability and requires a separate explicit policy decision.
- Measure equity/decision history after deduplication; compact/downsample only
  under a documented policy that preserves drawdown/equity evidence. Do not claim
  precision for discarded points.

Add continuous size, growth-rate and projected-days-to-reserve metrics, with an
alert and admission control before starting additional instance cohorts. Set
sampling/event budgets per instance; never silently reduce the existing strategy's
observation frequency. Benchmark the reference/equity growth of 1,000 instances
before enabling that fleet.

## 4. Backups after the redesign

Telegram remains disabled unless explicitly re-enabled. Replace full daily
snapshot-plus-archive multiplication with a reviewed incremental design: sealed
shared-input chunks uploaded once, small consistent ledger checkpoints, and a
versioned manifest connecting checkpoint, archive chunks and source release.
Retention must never remove a chunk referenced by any retained checkpoint.
Prove independent restore and complete reference coverage before scheduling it.

The first implementation priority is stage 1, then lossless conversion/compaction
in stage 2. Those stages address the measured duplication without depending on a
new database server or a change to strategy behavior.
