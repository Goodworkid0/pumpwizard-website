# PostgreSQL staging migration

`scripts/migrate_postgres.py` is an **operator-run staging importer**, not a switch
for the live execution backend. It never edits source SQLite databases, deletes
history, changes configs, starts trades, or points the running collector at
PostgreSQL. Runtime execution remains on SQLite by default. The separately tested
runtime adapter described below requires an explicit frozen-data cutover.

## What is preserved

Every non-system SQLite table's schema text and row values are retained. Text is
stored losslessly inside canonical JSON text; blobs use a tagged base64 value.
Original ID columns, contracts, source/code hashes, state, events, sale legs,
closed positions, equity marks, raw/shared inputs and input references remain
available. Unrecognized tables are archived too, not silently omitted.

Data lives in a dedicated schema (`pumpwizard_staging` by default), keyed by
immutable batch, source database, table and row ordinal. `instance_state`,
`instance_contracts`, `closed_positions` and `equity_marks` views expose useful
fields for long-hold instances. This is a lossless row-level staging representation,
not the final normalized high-throughput execution schema, and not a binary SQLite
backup replacement. Original backups must remain available.

Each source imports in one PostgreSQL transaction via COPY. Counts and row hashes
are checked before commit; complete ordered table hashes are verified afterward.
Repeating a verified batch/source skips it. Different source content under an
existing batch ID is refused, never overwritten. A failed source rolls back while
previously verified sources remain available for a resumed import.

## Rehearse, then cut over separately

1. Prepare a **frozen source directory** containing top-level `*.sqlite` backups.
   Do not use `cp` on live WAL databases. Use SQLite's backup API for a rehearsal.
   Such per-file online backups are not globally atomic: for a coordinated final
   migration, stop the collector, preserve the complete ledger/shared-input set,
   and verify that every input reference has its corresponding shared input.
2. Inspect without PostgreSQL writes:

   ```bash
   python3 scripts/migrate_postgres.py plan --source-dir /absolute/frozen-backup
   ```

3. Configure a private staging PostgreSQL connection using standard libpq settings
   (`PGSERVICE`/service file, `PGHOST`, `PGPORT`, `PGUSER`, `PGDATABASE`, and a protected
   password file). `psql` must be installed. Do not paste credentials into CLI
   arguments, repo files or public exports.
4. Import explicitly:

   ```bash
   python3 scripts/migrate_postgres.py import --source-dir /absolute/frozen-backup \
     --batch rehearsal_001 --schema pumpwizard_staging --apply --frozen-source
   python3 scripts/migrate_postgres.py verify --source-dir /absolute/frozen-backup \
     --batch rehearsal_001 --schema pumpwizard_staging
   ```

5. Compare balances, positions, P&L, fees and input-reference completeness; replay
   through the future PostgreSQL executor in shadow mode before considering a
   cutover. Do not dual-write live trading state without idempotency and recovery
   design. Keep SQLite as the source of truth throughout this stage.

The frozen-source flag is an operator assertion, not a service-stop command. This
tool does not verify that another process is stopped. Plan/verify scan the source
records and may be I/O intensive; run them against backups, not active ledgers.

Tested against an isolated PostgreSQL 15 cluster with synthetic records only:
`python3 tests/check_postgres_migration.py`. The test includes large binary inputs,
quoted/newline text, counts, ordered hashes, repeat imports, rollback and tampering.
No production connection or real-ledger import is performed by that test.

## Optional normalized runtime adapter

`paper/postgres_runtime.py` is distinct from the archival staging importer above.
`scripts/import_runtime.py` imports frozen runtime ledgers into a new namespace,
verifies every row, and refuses overwrites. Shared inputs are imported before
dependent ledgers. A writer advisory lease excludes a second collector; each step
commits state, input references, events, sale legs, positions and equity atomically.
Transactional IDs preserve SQLite-compatible rollback/retry results. There are no
dual writes and the immutable simulation source files are unchanged.

Activation requires `PUMP_LEDGER_BACKEND=postgres`, a private `PUMP_POSTGRES_DSN`
and `PUMP_POSTGRES_NAMESPACE`. Public serving needs separate read-only credentials
in `data/public-service.env`; collector credentials stay in `data/service.env`.
Do not enable either process before importing and validating the complete frozen
fleet. PostgreSQL is not implicitly provisioned or enabled by the worker deployment.

`tests/check_postgres_runtime.py` proves exact SQLite shadow state/table results,
duplicate-input handling, rollback IDs, writer exclusion, restart, read-only access,
independent pg_dump/pg_restore, and authoritative PG-to-SQLite backup/restore.
`tests/check_postgres_real_replay.py` also reproduced all 14,835 frozen observation
batches of `t2-persistent-combined`, matching its final state and four table proofs.
These are rehearsal results, not a claim of a completed production cutover or an
unlimited-capacity benchmark.

### Backup and rollback after cutover

SQLite files cease to be authoritative once PostgreSQL is enabled. Generic
`backup_lab.py` refuses that runtime. `backup_postgres_runtime.py` exports the fleet
and shared inputs from one PostgreSQL REPEATABLE READ snapshot into independently
restorable SQLite files. Other SQLite telemetry is backed up separately; this is
explicitly not globally atomic with the PG snapshot. Inventory, row counts, file
hashes and disk reserve are checked. Telegram daily backups select this exporter
when the public registry declares PostgreSQL, requiring private `postgres_credentials`
with `dsn` and `namespace` and psycopg installed in the backup environment.

For rollback, stop the PG writer first, export and verify its latest committed state,
restore into a new directory and check contracts/reference coverage. Never restart
an old SQLite mirror after PG has advanced. Preserve the original frozen directory,
PG database and rollback images until the recovery drill succeeds.
