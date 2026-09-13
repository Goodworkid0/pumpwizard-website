# Public launch operations

## What is enforced by the application

- Only public APIs are served. Legacy `/api/dashboard`, `/api/state`, account/ACP
  routes, the large `/api/public` compatibility response and `/acp` return 404.
- POST, PUT, PATCH and DELETE return 405. There is no web administration bypass
  or reusable admin token. Use reviewed local operator scripts for configuration
  changes; the old ACP UI is retired on this listener.
- Exact static asset mappings prevent serving the data directory or environment
  file. Host checks remain. External top-level links are allowed; cross-site
  scripted fetches are denied. No cross-origin API access is enabled.
- At most 32 request threads, a 32-connection accept backlog, 10-second socket
  timeouts and a 2,048-character request-target limit provide backend bounds.
  Saturated connections are closed so clients can retry. These limits do not
  replace a public reverse proxy or establish a tested capacity.

The Nginx HTTPS **template**, not a live deployment, now has public route
allowlists, connection limits, 10 requests/sec with a 30-request burst and separate
CSV limits of 2 requests/min with a burst of 2. Methods are read-only. It requires
rendering and `nginx -t` against the actual installed version before reload.
Do not expose Python's HTTP listener directly to the Internet. Replacing it with
a dedicated production serving process remains a separate infrastructure step.

Rate-limit zones belong in the Nginx `http` context. They use the socket peer IP.
Before enabling Cloudflare proxying, configure real-IP handling with trusted,
current Cloudflare ranges at the origin; otherwise multiple visitors share an
edge-IP bucket. Never trust arbitrary forwarded headers. TLS and DNS are unchanged
by this work. No remote proxy has been reloaded automatically.

## Readiness and monitoring

`GET /api/public/health` returns 200 only if all checks pass; otherwise 503:

| Check | Healthy condition |
|---|---|
| Snapshot | Last build succeeded and source age is at most 45 seconds |
| Snapshot worker | Publication thread is alive |
| Simulation cycle | A full collector/strategy cycle finished within 180 seconds |
| Market input | The latest cycle contains an observation no older than 300 seconds |
| Background workers | Both configured brain threads are alive |

Startup may be degraded until the first full cycle and snapshot complete. An empty
market batch is conservatively degraded. No trade is required for readiness. Input
age describes the observations received by the lab, not proof that every upstream
quote was fresh on-chain. Worker liveness is not a full per-worker progress check.
Readiness never performs a database query or reveals provider errors or paths.
Docker health checks use this endpoint; unhealthy status alone does not restart
a Docker container. Avoid restart loops for upstream outages.

```bash
python3 scripts/check_lab.py
python3 scripts/check_lab.py --backup-root /approved/backup-parent
```

The probe emits JSON and exits 1 on degraded/unreachable service, less than 10 GiB
or 10% free disk, or no completed backup within 24 hours when a backup parent is
provided. Backup age checks **only the completed manifest**, not every file hash.
Run the full verify command after copying an archive and during restore drills.
Without `--backup-root`, the output explicitly says backup monitoring is absent.

`deploy/pump-lab-check.service` and `.timer` are now installed on this host. They
run the standalone `scripts/monitor_lab.py` every two minutes using the isolated
backup runtime. Readiness, disk, 36-hour backup freshness, stalled transfer progress
and the initial production restore marker feed debounced Telegram alerts to the
existing private backup channel. Three matching failures trigger an alert, followed
by hourly reminders and one recovery notice. See [Telegram backups](telegram-backups.md).
An external uptime monitor is still required to detect a fully offline host.

## Verified backup and restore rehearsal

Choose an existing backup parent outside the live checkout, preferably on a
separate device. Each destination must be new; nothing is overwritten or pruned.

```bash
python3 scripts/backup_lab.py plan --destination /approved/backups/run-001
python3 scripts/backup_lab.py create --destination /approved/backups/run-001
python3 scripts/backup_lab.py verify --source /approved/backups/run-001
python3 scripts/backup_lab.py restore --source /approved/backups/run-001 --destination /approved/restores/drill-001
```

The tool discovers every top-level `data/*.sqlite`, including inactive ledgers,
and a fixed allowlist of 14 configuration/experiment JSON files. SQLite's backup
API includes committed WAL state without copying a moving database file. Each copy
is checkpointed into a standalone file, quick-checked, row-counted by table and
SHA-256 hashed. Copies and their completion manifest are fsynced. Config hashes
and source inventory must remain stable across the run. Symlinks, unexpected
archive files, checksum mismatches and reused destination paths are rejected.
Default per-database copy and integrity-query budgets are five minutes each.

`data/service.env`, provider credentials, runtime lock files, cached AI briefs,
source code and operating-system configuration are intentionally not included.
Keep a matching code revision/archive and separately secured secret recovery plan.
The backup is private operational data, not a public research download. SHA-256
detects corruption; it is not encryption or authentication against someone who
can replace both files and manifest.

Online backups are **individually consistent, not cross-ledger atomic**. They are
useful for retention and drills, but a final production cutover backup should be
taken with every writer stopped. `--frozen-source` only records the operator's
assertion; it does not stop or verify processes. Coordinate downtime separately.
The PostgreSQL staging importer can read a frozen archive's `data/` directory.

Restore copies into `data/` and `config/` under a brand-new rehearsal directory,
then verifies hashes, SQLite integrity and every table count again. It never
starts a collector or reconnects to market providers. This proves record-level
restorability, not complete application restart/replay reconciliation. For a real
recovery, stop all writers, restore separately, place config files at the matching
code root, validate contracts/accounting, and switch directories explicitly. Do
not merge an archive over a running database/WAL pair.

Failed runs leave the new directory for inspection, without a completion manifest
unless copying/checks finished. The generic snapshot tool does not prune; the
separate Telegram workflow supports explicitly approved verified retention. Do not
count a second directory on the same disk as off-site recovery. Select storage,
encryption, cadence, retention and alert delivery before scheduling real backups.

## Still required before broad release

Update 2026-09-13: the operator explicitly paused Telegram backups pending the
[storage rework](storage-rework-plan.md). Both configurations now set
`backup_enabled: false`; production backup scheduling is disabled. The monitor
continues checking readiness and disk while marking backup checks disabled.

1. Resolve the next full backup's measured capacity shortfall. After approved
   retention, roughly 103 GiB was free while the 49 GiB live source required about
   155 GiB including reserve. Measurements change as collection continues;
   `monitor_lab.py` reports `next_backup_capacity` using the daily job's budget.
   Do not reduce reserve or delete live history to make the check pass.
2. Confirm recovery keys and credentials are secured off-host. The independent
   watchdog currently depends on the operator workstation remaining online.
3. Sustained read/collector load tests and failure-recovery tests at target scale.
4. Explicit PostgreSQL production provisioning/cutover, if moving beyond SQLite.
   The normalized adapter and synthetic/real-history shadow replay now exist;
   they do not automatically switch the production database.

## Hardening release — 2026-09-13

The candidate adds a separate read-only public worker, incremental chart caching,
reviewed replay-archive downloads, checkpoint-era verification, and an opt-in
PostgreSQL runtime with independently tested recovery. `scripts/promote_workers.py`
pins the image ID, checks unchanged simulation code, verified production recovery,
disk reserve and a successful staging soak. It retains the previous container and
image; neither instance configuration nor ledger history is reset. Database cutover
is deliberately separate. See the host release's `worker-deployment.json` for the
actual deployment phase, not this documentation as a claim that promotion finished.

An independent user-systemd watchdog on the operator workstation checks public
HTTPS every two minutes and sends debounced failure/recovery messages through the
existing bot. Linger is enabled, but this is only independent while that workstation
is online. The monitor does not acquire the MTProto backup session or collector lock.

The approved local retention policy keeps the latest verified recovery copy and
all Telegram backups. It removes only hash-matching older or duplicate local backup
payloads after re-verifying the retained copy. Live ledgers, source releases,
Telegram parts, receipts and rollback containers are never pruned. Backup preflight
reserves at least 10 GiB or 10% of disk and refuses insufficient recovery headroom.
This is not infinite capacity; review measured growth before scaling the fleet.
Keep the content key and recovery credentials off-host; Telegram alone is not a
recoverable backup without them.

Production promotion completed on 2026-09-13. The first attempt rolled back safely
after a Compose tmpfs parsing error; the corrected retry passed and retained all
251 stored instance histories. Both workers are healthy, and the public worker's
data mount is read-only. Public browser checks passed for all six page layouts,
paging, comparisons, complete-history exports, archive byte ranges and mobile UI.
The subsequent five-minute production HTTPS health/catalog observation had zero
failures and 127.89 ms p95 response time; this is a low-rate check, not a load SLA.
The production Telegram restore verified 160 parts and reconciled 251 frozen
instances (7,973 closed and 879 open positions). Approved retention reclaimed
66,414,137,752 bytes without deleting any Telegram backup. The retained local
recovery copy is `production-restore-20260913T191951Z` under the backup root.
These checks do not establish exact historical replay, 1,000-instance production
capacity, or sufficient space for the next full backup. PostgreSQL remains opt-in;
the deployed ledger backend is SQLite.
