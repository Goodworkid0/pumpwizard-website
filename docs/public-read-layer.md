# Public read isolation

The public API publishes an in-memory read model on a background thread. Report
queries use **separate query-only SQLite connections**, loading committed state
from `meta.state`. They do not query the simulator's connections or take its writer
locks. Reporting code is reused so accounting definitions do not drift. Read
transactions close after each report/query group; results across instances are
not an atomic global snapshot.

Publication targets a 15-second cadence including build time, without overlapping
builds (at least one second between builds if a build exceeds the interval). The previous
publication stays available if a build fails. Responses include `_snapshot` with
the source time, publication time, age and stale/error flags. Data is marked stale
after 45 seconds or a failed build, and every public page shows freshness. Before
the first successful build the API returns 503 with Retry-After. There is no
request-triggered rebuild or cache-stampede fallback.

## Endpoints

| Endpoint | Behavior |
|---|---|
| `/api/public/summary` | Live View's compact reports, portfolio, positions and latest closed records |
| `/api/public/catalog` | Server filtering/sorting, default 24 / maximum 100 instances per page |
| `/api/public/strategies/<id>` | One full instance, its selected comparison family summaries and open positions |
| `/api/public/strategies/<id>/trades` | Keyset-paged closed records; default 100 / maximum 200 per request |
| `/api/public/live-feed` | Prebuilt latest activity, without the full portfolio payload |
| `/api/public/research` | Prebuilt research counters, coverage and recent events |
| `/api/public/community` | Cached public contact/community links |
| `/api/public/health` | Constant-time readiness; 503 for stale snapshots, inputs or stopped workers |

The former `/api/public` compatibility payload and legacy/operator APIs now return
404 on the HTTP listener. Use the endpoints above, not direct simulator reports.

Catalog parameters: `q`, `family`, `status`, `performance`, `version`, `sort`,
`page`, `page_size`, and up to four comma-separated `compare` IDs. Unknown metrics
sort after known values, including known zero win rates. Global activity/family
counts remain independent of the selected page. Detailed configuration/telemetry
is fetched only when a visitor requests a copy or download.

History parameters: `limit`, `before`, `snapshot_max_id`. The first response fixes
the maximum trade ID and returns `total` and `next_before`. Pass the same maximum
and the returned cursor for every subsequent page. New closures are excluded from
that boundary. The instance table renders 25 rows per page; analysis loads complete
bounded evidence only when requested. Browser research exports stop at 10,000
records, 20 MB or 60 seconds, and validate identity, cursor progress, duplicates,
boundary and total count. These limits produce an error, not a partial download.

History queries use independent read-only connections, a three-second SQL budget,
and four concurrent slots. Public CSV exports permit at most 10,000 combined rows
and 20 MB, one preparation at a time, with a one-minute result/error cache. Larger
datasets return 413 and require an operator-prepared archive. Existing CLI export
tools remain available to the operator. No existing data is pruned.

## Deployment and limits

The service/Compose file-descriptor allowance is 8,192. The current one-database-
per-instance layout already needs many files, including WAL, SHM and lock files;
additional SQLite handles may be retained while the writer connection stays open.
This allowance is headroom, not proof of 1,000-instance capacity.

`deploy/compose.workers.yml` separates the collector from `paper.public_worker`.
The collector atomically publishes a bounded registry every five seconds; the
public process builds reports from independent read-only connections, without
initializing a runner, provider or writer. Its filesystem and data mount are
read-only, it receives no collector credentials, and stale registry heartbeats
degrade readiness. The processes still share host disk and finite CPU resources.
Incremental equity-bucket caching avoids repeatedly scanning complete histories.
The optional PostgreSQL adapter uses separate read-only credentials and query
deadlines; SQLite remains the default. Long-running historical scans and
publication time still need monitoring/benchmarking. The application rejects
operator routes and all write methods; connections have timeouts and a 32-request
concurrency cap. Production must remain behind the allowlisting/rate-limiting proxy.
See [launch operations](launch-operations.md) for health thresholds, backup/restore
tools and guarded deployment steps. Worker isolation is not a sustained-load
capacity claim. The release script verifies immutable simulation hashes, retains
the old container/image and checks history continuity before switching routes.
